"""材料包（ZIP）导入：若干扫描件 PDF + 一份带 PDF 文件名的授权 CSV。

规则：
- 包内 PDF 先逐个流式落盘并按 SHA-256 去重；相同内容只保存一份。
- CSV 沿用原列（授权号、原图号、是否允许商用、署名），另加「PDF文件名」列。
- 按文件名绑定包内原件；多条授权引用同一份 PDF 时共用同一份存储文件。
- 授权号已存在时必须核对原图、商用标记、署名一致且 PDF 内容相同才可补挂原件；
  任一不符即整包失败并说明原因（同号冲突）。已挂原件的授权再次导入视为重传，
  需核对全部 PDF 内容完全相同；重传/补件都不会恢复已撤回的授权。
- 缺件、同号冲突、CSV 错误都会使整包不生效：数据库事务回滚，已落盘的新文件
  由调用方在 catch 中删除。
- 全部校验通过后，新授权与原件的挂接才随同一事务入库；提交前 sync 不会
  让新授权进入展图清单。
"""
import csv
import io
import os
import zipfile
from typing import Dict, List

from . import storage
from .logic import HEADER_ALIASES, _get_or_create_original, parse_commercial


MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 5000


class PackageError(ValueError):
    """材料包校验失败：整包不生效，消息即为对外原因。"""


def _norm_name(name: str) -> str:
    """统一包内文件名：去掉目录前缀，兼容 Windows 反斜杠。"""
    name = name.replace("\\", "/")
    return name.rsplit("/", 1)[-1].strip()


def _read_csv_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size > MAX_CSV_BYTES:
        raise PackageError("包内 CSV 过大")
    with zf.open(info) as f:
        data = f.read(MAX_CSV_BYTES + 1)
    if len(data) > MAX_CSV_BYTES:
        raise PackageError("包内 CSV 过大")
    return data


def _parse_license_rows(content: bytes) -> List[dict]:
    """解析授权 CSV，返回行列表；任何结构性问题抛 PackageError。"""
    if b"\x00" in content[:4096]:
        raise PackageError("授权 CSV 不是文本文件")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("gb18030")
        except UnicodeDecodeError:
            raise PackageError("授权 CSV 无法按 UTF-8 或 GB18030 解码")
    try:
        rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    except csv.Error as e:
        raise PackageError(f"授权 CSV 解析失败：{e}")
    if not rows:
        raise PackageError("包内授权 CSV 内容为空")
    header = [h.strip().lower() for h in rows[0]]
    mapping = {}
    for canon, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias.lower() in header:
                mapping[canon] = header.index(alias.lower())
                break
    required = ["license_no", "original_no", "commercial", "attribution", "pdf_file"]
    missing = [k for k in required if k not in mapping]
    if missing:
        cn = {"license_no": "授权号", "original_no": "原图号", "commercial": "是否允许商用",
              "attribution": "署名", "pdf_file": "PDF文件名"}
        raise PackageError(
            "授权 CSV 缺少必需列：" + "、".join(cn[k] for k in missing)
            + "（材料包 CSV 在原有列之外需含 PDF文件名 列）")

    def cell(row, canon):
        idx = mapping[canon]
        return row[idx].strip() if idx < len(row) else ""

    parsed = []
    by_license = {}
    for i, row in enumerate(rows[1:], start=2):
        try:
            lic_no = cell(row, "license_no")
            orig_no = cell(row, "original_no")
            commercial = parse_commercial(cell(row, "commercial"))
            attribution = cell(row, "attribution")
            pdf_name = cell(row, "pdf_file")
            if not lic_no:
                raise ValueError("授权号为空")
            if not orig_no:
                raise ValueError("原图号为空")
            if not attribution:
                raise ValueError("署名为空")
            if not pdf_name:
                raise ValueError("PDF文件名为空")
        except ValueError as e:
            raise PackageError(f"授权 CSV 第{i}行：{e}")
        item = {"line": i, "license_no": lic_no, "original_no": orig_no,
                "commercial": commercial, "attribution": attribution,
                "pdf_name": _norm_name(pdf_name)}
        prev = by_license.get(lic_no)
        if prev is not None:
            # 同号多行：仅当原图、商用标记、署名完全一致时才视为同一授权
            # 的多份扫描件；否则属同号冲突，整包不生效
            if (prev["original_no"] != orig_no
                    or prev["commercial"] != commercial
                    or prev["attribution"] != attribution):
                raise PackageError(
                    f"授权 CSV 内授权号 {lic_no} 重复且信息不一致"
                    f"（第{prev['line']}行与第{i}行），同号冲突，整包不生效")
            if item["pdf_name"].lower() in {p.lower() for p in prev["pdf_names"]}:
                raise PackageError(
                    f"授权 CSV 第{i}行：授权号 {lic_no} 重复引用同一扫描件 "
                    f"{item['pdf_name']!r}，同号冲突，整包不生效")
            prev["pdf_names"].append(item["pdf_name"])
            continue
        prev = dict(item, pdf_names=[item["pdf_name"]])
        by_license[lic_no] = prev
        parsed.append(prev)
    return parsed


def _extract_pdfs(zf: zipfile.ZipFile, staging_dir: str,
                  cancel=None) -> Dict[str, dict]:
    """流式解压全部 PDF，返回 {小写文件名: {sha256, size, staged_path, filename}}。

    包内同名文件、路径穿越、非 PDF、超限解压均拒绝，并说明原因。
    """
    members = [i for i in zf.infolist() if not i.is_dir()]
    if len(members) > MAX_MEMBERS:
        raise PackageError(f"包内文件过多（{len(members)}），超过上限 {MAX_MEMBERS}")
    result = {}
    max_bytes = storage.max_pdf_bytes()
    total_limit = storage.max_package_bytes()
    declared = sum(i.file_size for i in members)
    if declared > total_limit:
        raise PackageError(
            f"材料包解压后大小（{declared // (1024*1024)} MiB）超过上限 "
            f"{total_limit // (1024*1024)} MiB")
    extracted = 0
    for info in members:
        if info.filename.endswith("/"):
            continue
        name = _norm_name(info.filename)
        lower = name.lower()
        if not lower.endswith(".pdf"):
            continue
        parts = info.filename.replace("\\", "/").split("/")
        if ".." in parts or name in ("", ".", ".."):
            raise PackageError(f"包内文件名非法：{info.filename!r}")
        if lower in result:
            raise PackageError(f"包内存在同名扫描件（忽略路径/大小写）：{name}")
        if info.flag_bits & 0x1:
            raise PackageError(f"扫描件 {name} 已加密，无法读取")
        staged = os.path.join(staging_dir, f"{len(result)}.part")
        try:
            with zf.open(info) as src:
                sha, size = storage.copy_stream_hashed(
                    src, staged, max_bytes=max_bytes, cancel=cancel)
        except ValueError as e:
            raise PackageError(f"扫描件 {name}：{e}")
        except (zipfile.BadZipFile, RuntimeError) as e:
            raise PackageError(f"扫描件 {name} 解压失败：{e}")
        with open(staged, "rb") as f:
            if f.read(5) != b"%PDF-":
                raise PackageError(f"扫描件 {name} 不是 PDF 文件（缺少 %PDF- 头）")
        extracted += size
        if extracted > total_limit:
            raise PackageError(
                f"材料包解压后大小超过上限 {total_limit // (1024*1024)} MiB")
        result[lower] = {"sha256": sha, "size": size, "staged_path": staged,
                         "filename": name}
    return result


def inspect_like_zip(path: str) -> bool:
    return zipfile.is_zipfile(path)


def stage_package(zip_path: str, staging_dir: str, cancel=None):
    """把材料包解压/校验到暂存目录，不触碰数据库（可在无写锁时耗时执行）。

    返回 ``(rows, pdfs)``；缺件、同号冲突、非 PDF 等在入库前即被拒绝。
    """
    storage.check_cancel(cancel)
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise PackageError("上传的文件不是有效的 ZIP 压缩包")
    with zf:
        csv_infos = [i for i in zf.infolist()
                     if not i.is_dir() and _norm_name(i.filename).lower().endswith(".csv")]
        if not csv_infos:
            raise PackageError("材料包内没有授权 CSV")
        if len(csv_infos) > 1:
            names = "、".join(_norm_name(i.filename) for i in csv_infos)
            raise PackageError(f"材料包内有多份 CSV（{names}），无法确定使用哪一份")
        csv_info = csv_infos[0]
        rows = _parse_license_rows(_read_csv_member(zf, csv_info))
        if not rows:
            raise PackageError("授权 CSV 没有数据行")
        # 先核对文件名与 CSV 引用是否对得上，缺件无需解压即可报告
        names = {_norm_name(i.filename).lower() for i in zf.infolist()
                 if not i.is_dir() and _norm_name(i.filename).lower().endswith(".pdf")}
        for r in rows:
            for pdf_name in r["pdf_names"]:
                if pdf_name.lower() not in names:
                    raise PackageError(
                        f"第{r['line']}行引用的扫描件 {pdf_name!r} 在包内不存在（缺件）")
        pdfs = _extract_pdfs(zf, staging_dir, cancel=cancel)

    referenced = []
    for r in rows:
        for pdf_name in r["pdf_names"]:
            referenced.append((r, pdfs[pdf_name.lower()]))
    storage.check_cancel(cancel)
    return referenced, pdfs


def commit_staged(conn, referenced, pdfs, batch_label: str = "",
                  published: List[str] = None) -> dict:
    """把已解压暂存的材料包写入当前事务（调用方负责 BEGIN/commit/rollback）。"""
    if published is None:
        published = []
    return _commit_rows(conn, referenced, pdfs, batch_label, published)


def remove_published(published: List[str]) -> None:
    """导入失败时删除本次新发布、且未被数据库引用的扫描件文件。

    删除前再查一次 files 表：若相同内容已由其他批次入库（并发导入），保留文件。
    """
    if not published:
        return
    from . import db
    conn = db.connect()
    try:
        for path in published:
            sha = os.path.basename(path)[:-len(".pdf")]
            if conn.execute("SELECT 1 FROM files WHERE sha256=?", (sha,)).fetchone():
                continue
            try:
                os.remove(path)
            except OSError:
                pass
    finally:
        conn.close()


def _license_conflicts(conn, lic_no: str, row) -> List[str]:
    """核对已有授权号的原图、商用标记与署名，返回不一致的原因。"""
    existing = conn.execute(
        "SELECT * FROM licenses WHERE license_no=?", (lic_no,)).fetchone()
    if existing is None:
        return []
    reasons = []
    cur = conn.execute("SELECT original_no FROM originals WHERE id=?",
                       (existing["original_id"],)).fetchone()
    cur_no = cur["original_no"] if cur else ""
    if cur_no != row["original_no"]:
        reasons.append(f"原图号不同（已存 {cur_no or '?'}，本包 {row['original_no']}）")
    if existing["commercial"] != row["commercial"]:
        reasons.append("商用标记不同")
    if existing["attribution"] != row["attribution"]:
        reasons.append("署名不同")
    return reasons


def _linked_shas(conn, license_id: int) -> List[str]:
    return [r["sha256"] for r in conn.execute(
        "SELECT f.sha256 FROM license_files lf JOIN files f ON f.id = lf.file_id"
        " WHERE lf.license_id=? ORDER BY f.sha256", (license_id,)).fetchall()]


def _commit_rows(conn, referenced, pdfs, batch_label, published) -> dict:
    stats = {"pdfs": len(pdfs), "new_files": 0, "new_licenses": 0,
             "linked": 0, "unreferenced": 0,
             "skipped_revoked": 0, "notes": []}

    # 1) 按授权号归组，先校验全部同号规则，再做任何写入：
    #    同号冲突 / 重传内容不符都必须让整包失败
    grouped = {}
    for row, pdf in referenced:
        grouped.setdefault(row["license_no"], {"row": row, "shas": []})
        item = grouped[row["license_no"]]
        reasons = _license_conflicts(conn, row["license_no"], row)
        if reasons:
            raise PackageError(
                f"授权号 {row['license_no']} 与已有授权冲突：{'；'.join(reasons)}，整包不生效")
        if pdf["sha256"] not in item["shas"]:
            item["shas"].append(pdf["sha256"])

    plan = []
    revoked_hits = set()
    for lic_no, item in grouped.items():
        row, shas = item["row"], sorted(item["shas"])
        existing = conn.execute(
            "SELECT * FROM licenses WHERE license_no=?", (lic_no,)).fetchone()
        if existing is None:
            plan.append((row, shas, None, "new"))
            continue
        if existing["revoked"]:
            # 已撤回为终态：不补挂、不重传、不恢复；仅提示
            revoked_hits.add(lic_no)
            continue
        linked = _linked_shas(conn, existing["id"])
        if linked:
            if sorted(linked) != shas:
                raise PackageError(
                    f"授权号 {lic_no} 已挂接 {len(linked)} 份扫描件，本包为 "
                    f"{len(shas)} 份且内容不完全相同，重传未通过内容核对，整包不生效")
            plan.append((row, shas, existing, "reupload"))
        else:
            plan.append((row, shas, existing, "backfill"))

    stats["unreferenced"] = len({p["sha256"] for p in pdfs.values()}
                                - {s for it in grouped.values() for s in it["shas"]})
    for lic_no in sorted(revoked_hits):
        stats["skipped_revoked"] += 1
        stats["notes"].append(f"授权号 {lic_no} 已撤回，保留撤回状态，不补挂原件")

    by_sha = {}
    for p in pdfs.values():
        by_sha.setdefault(p["sha256"], p)

    # 2) 发布被引用的扫描件（内容寻址；相同 SHA 只落一份）
    file_ids = {}
    for sha in sorted(by_sha):
        if not any(sha in shas for _, shas, _, _ in plan):
            continue
        pdf = by_sha[sha]
        dest = storage.final_path(sha)
        if not os.path.exists(dest):
            storage.publish(pdf["staged_path"], sha)
            published.append(dest)
        row = conn.execute("SELECT id FROM files WHERE sha256=?", (sha,)).fetchone()
        if row:
            file_id = row["id"]
            conn.execute("UPDATE files SET filename=? WHERE id=?",
                         (pdf["filename"], file_id))
        else:
            file_id = conn.execute(
                "INSERT INTO files (sha256, size, filename) VALUES (?,?,?)",
                (sha, pdf["size"], pdf["filename"])).lastrowid
            stats["new_files"] += 1
        file_ids[sha] = file_id

    # 3) 入库授权并挂接原件（新授权此刻才可见；提交前不会进入展图清单）
    for row, shas, existing, kind in plan:
        if existing is None:
            orig_id = _get_or_create_original(conn, row["original_no"])
            lic_id = conn.execute(
                "INSERT INTO licenses (license_no, original_id, commercial, attribution,"
                " batch_label) VALUES (?,?,?,?,?)",
                (row["license_no"], orig_id, row["commercial"], row["attribution"],
                 batch_label)).lastrowid
            stats["new_licenses"] += 1
        else:
            lic_id = existing["id"]
            if kind == "reupload":
                continue  # 内容已逐份核对相同，无需重复挂接
        for sha in shas:
            cur = conn.execute(
                "INSERT OR IGNORE INTO license_files (license_id, file_id) VALUES (?,?)",
                (lic_id, file_ids[sha]))
            if cur.rowcount:
                stats["linked"] += 1

    batch_id = conn.execute(
        "INSERT INTO import_batches (batch_label, state, pdf_count, new_files,"
        " new_licenses, linked) VALUES (?,?,?,?,?,?)",
        (batch_label, "imported", stats["pdfs"], stats["new_files"],
         stats["new_licenses"], stats["linked"])).lastrowid
    stats["batch_id"] = batch_id
    return stats


def mark_batch_committed(conn, batch_id: int) -> None:
    conn.execute("UPDATE import_batches SET state='committed' WHERE id=?", (batch_id,))
