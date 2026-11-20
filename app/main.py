"""FastAPI 网页与接口：展图授权清单管理。"""
import asyncio
import csv
import io
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db, logic, package_import, storage

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES)

MAX_UPLOAD_ZIP_MB = int(os.environ.get("EXHIBITION_MAX_ZIP_MB", "2048"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    storage.cleanup_stale()
    yield


app = FastAPI(title="展图授权清单管理", lifespan=lifespan)


def redirect(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    params = {}
    if msg:
        params["msg"] = msg
    if err:
        params["err"] = err
    if params:
        url = f"{url}?{urlencode(params)}"
    return RedirectResponse(url, status_code=303)


def flash_of(request: Request) -> dict:
    return {"msg": request.query_params.get("msg", ""),
            "err": request.query_params.get("err", "")}


def get_exhibition_or_404(conn, ex_id: int):
    ex = conn.execute("SELECT * FROM exhibitions WHERE id=?", (ex_id,)).fetchone()
    if ex is None:
        raise HTTPException(status_code=404, detail="展图不存在")
    return ex


# ---------------------------------------------------------------- 页面

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        logic.sync_all(conn)
        rows = conn.execute(
            "SELECT e.*, (SELECT COUNT(*) FROM exhibition_originals eo"
            " WHERE eo.exhibition_id=e.id) AS total"
            " FROM exhibitions e ORDER BY e.exhibition_no"
        ).fetchall()
        exhibitions = []
        for ex in rows:
            st = logic.exhibition_status(conn, ex["id"])
            exhibitions.append({
                "id": ex["id"], "exhibition_no": ex["exhibition_no"], "title": ex["title"],
                "total": ex["total"], "downloadable": st["downloadable"],
                "missing": st["missing"], "unconfirmed": st["unconfirmed"],
            })
        conn.commit()
    finally:
        conn.close()
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"exhibitions": exhibitions, **flash_of(request)})


@app.get("/exhibitions/{ex_id}", response_class=HTMLResponse)
def exhibition_detail(request: Request, ex_id: int):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ex = get_exhibition_or_404(conn, ex_id)
        st = logic.exhibition_status(conn, ex_id)
        for it in st["items"]:
            it["alternatives"] = [dict(a) for a in conn.execute(
                "SELECT license_no, attribution FROM licenses"
                " WHERE original_id=? AND commercial=1 AND revoked=0 AND id != ?"
                " ORDER BY id",
                (it["original_id"], it["license_id"] or -1),
            ).fetchall()]
        confirmations = [dict(r) for r in conn.execute(
            "SELECT editor, detail, created_at FROM confirmations"
            " WHERE exhibition_id=? ORDER BY id DESC LIMIT 10", (ex_id,),
        ).fetchall()]
        conn.commit()
    finally:
        conn.close()
    return templates.TemplateResponse(
        request=request, name="exhibition.html",
        context={"ex": dict(ex), "st": st, "confirmations": confirmations,
                 **flash_of(request)})


@app.get("/licenses", response_class=HTMLResponse)
def licenses_page(request: Request):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        logic.sync_all(conn)
        rows = conn.execute(
            "SELECT l.*, o.original_no,"
            " GROUP_CONCAT(DISTINCT e.exhibition_no) AS used_by"
            " FROM licenses l"
            " JOIN originals o ON o.id = l.original_id"
            " LEFT JOIN manifest_items mi ON mi.license_id = l.id"
            " LEFT JOIN exhibitions e ON e.id = mi.exhibition_id"
            " GROUP BY l.id ORDER BY l.id"
        ).fetchall()
        file_rows = conn.execute(
            "SELECT lf.license_id, f.id, f.filename, f.sha256"
            " FROM license_files lf JOIN files f ON f.id = lf.file_id"
            " ORDER BY lf.license_id, f.id").fetchall()
        conn.commit()
    finally:
        conn.close()
    by_license = {}
    for fr in file_rows:
        by_license.setdefault(fr["license_id"], []).append(dict(fr))
    licenses = []
    for r in rows:
        d = dict(r)
        d["file_list"] = by_license.get(d["id"], [])
        licenses.append(d)
    return templates.TemplateResponse(
        request=request, name="licenses.html",
        context={"licenses": licenses, **flash_of(request)})


# ---------------------------------------------------------------- 导入

@app.post("/upload/mapping")
async def upload_mapping(file: UploadFile):
    content = await file.read()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            stats = logic.import_mapping(conn, content)
        except ValueError as e:
            conn.rollback()
            return redirect("/", err=f"对照 CSV 导入失败：{e}")
        logic.sync_all(conn)
        conn.commit()
    finally:
        conn.close()
    msg = f"对照导入完成：{stats['rows']} 行，新增 {stats['links_added']} 条对照关系"
    if stats["errors"]:
        return redirect("/", err=msg + "；部分行出错：" + "；".join(stats["errors"][:5]))
    return redirect("/", msg=msg)


@app.post("/upload/licenses")
async def upload_licenses(request: Request, file: UploadFile):
    form = await request.form()
    batch = str(form.get("batch", "")).strip() or (file.filename or "")
    content = await file.read()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            stats = logic.import_licenses(conn, content, batch)
        except ValueError as e:
            conn.rollback()
            return redirect("/", err=f"授权 CSV 导入失败：{e}")
        logic.sync_all(conn)
        conn.commit()
    finally:
        conn.close()
    msg = f"授权导入完成：新增 {stats['added']} 条"
    if stats["duplicates"]:
        msg += f"，跳过已存在授权号 {stats['duplicates']} 条（重传不会恢复已撤回的授权）"
    if stats["errors"]:
        return redirect("/", err=msg + "；部分行出错：" + "；".join(stats["errors"][:5]))
    return redirect("/", msg=msg)


async def _save_upload_to_temp(file: UploadFile) -> str:
    """把上传的 ZIP 按块写入暂存文件，避免整包读入内存。"""
    path = storage.new_staging_file(".zip")
    total = 0
    limit = MAX_UPLOAD_ZIP_MB * 1024 * 1024
    try:
        with open(path, "wb") as out:
            while True:
                chunk = await file.read(storage.CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise package_import.PackageError(
                        f"材料包超过大小上限 {MAX_UPLOAD_ZIP_MB} MiB")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


def _run_package_import(zip_path: str, batch: str, cancel=None) -> dict:
    """在校验线程中执行整包导入：先在锁外解压暂存，再在单个写事务内入库。

    返回 ``{"ok": True, "stats": ...}`` 或 ``{"ok": False, "error": 原因}``；
    意外异常按“整包不生效”回滚后向调用方抛出。``cancel`` 每处理一块调用一次，
    客户端断开时由监视协程置位，导入随即中止并回滚。
    """
    published = []
    staging_dir = storage.new_staging_dir()
    try:
        try:
            referenced, pdfs = package_import.stage_package(
                zip_path, staging_dir, cancel=cancel)
        except package_import.PackageError as e:
            return {"ok": False, "error": str(e)}
        except storage.Cancelled:
            return {"ok": False, "error": "保存中断（客户端已断开），整包未生效"}

        conn = db.connect()
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as e:
                return {"ok": False, "error": f"数据库忙，请重试（{e}）"}
            try:
                stats = package_import.commit_staged(
                    conn, referenced, pdfs, batch, published)
                package_import.mark_batch_committed(conn, stats["batch_id"])
                logic.sync_all(conn)  # 新授权与原件一并落库后，才可用于展图清单
                conn.commit()
                return {"ok": True, "stats": stats}
            except package_import.PackageError as e:
                conn.rollback()
                return {"ok": False, "error": str(e)}
            except storage.Cancelled:
                conn.rollback()
                return {"ok": False, "error": "保存中断（客户端已断开），整包未生效"}
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()
            # 成功提交后 files 表已引用这些文件，删除会被跳过；失败/中断时清理孤儿
            package_import.remove_published(published)
    finally:
        storage.discard_dir(staging_dir)


@app.post("/upload/package")
async def upload_package(request: Request, file: UploadFile):
    """ZIP 材料包导入：整包成功或整包不生效。

    上传先按块落到暂存文件；校验与入库在写事务内完成。任何失败（缺件、
    同号冲突、CSV 错误、客户端中断、保存异常）都会回滚数据库，并删除本次
    新发布、未被引用的扫描件，重启后可直接重传。
    """
    form = await request.form()
    batch = str(form.get("batch", "")).strip() or (file.filename or "材料包")
    zip_path = None
    try:
        try:
            zip_path = await _save_upload_to_temp(file)
        except package_import.PackageError as e:
            return redirect("/", err=f"材料包导入失败：{e}")
        except asyncio.CancelledError:
            return Response(status_code=499)

        if not package_import.inspect_like_zip(zip_path):
            return redirect("/", err="材料包导入失败：上传的文件不是有效的 ZIP 压缩包")

        # 导入在线程池中执行；其间轮询客户端连接。断开即置取消标记，
        # 导入在写事务内中止并回滚，不留下任何数据或孤儿扫描件。
        cancel_event = threading.Event()
        task = asyncio.get_running_loop().run_in_executor(
            None, _run_package_import, zip_path, batch, cancel_event.is_set)
        disconnected = False
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.5)
                if done:
                    break
                if await request.is_disconnected():
                    disconnected = True
                    cancel_event.set()
                    await asyncio.wait({task}, timeout=60)
                    break
        except asyncio.CancelledError:
            # 服务取消本请求：同样让后台导入尽快回滚，等它释放数据库后再退出
            cancel_event.set()
            try:
                await asyncio.wait({task}, timeout=5)
            except BaseException:
                pass
            raise
        try:
            outcome = task.result()
        except asyncio.CancelledError:
            return Response(status_code=499)
        except Exception:
            return redirect("/", err="材料包导入失败（整包未生效）：保存中断，请重试")
        if disconnected:
            return Response(status_code=499)
    except asyncio.CancelledError:
        return Response(status_code=499)
    finally:
        if zip_path:
            try:
                os.remove(zip_path)
            except OSError:
                pass

    if not outcome["ok"]:
        return redirect("/", err=f"材料包导入失败（整包未生效）：{outcome['error']}")
    stats = outcome["stats"]
    msg = (f"材料包导入完成：扫描件 {stats['pdfs']} 个（新存 {stats['new_files']} 份），"
           f"新增授权 {stats['new_licenses']} 条，补挂原件 {stats['linked']} 条")
    if stats["unreferenced"]:
        msg += f"；{stats['unreferenced']} 个扫描件未被 CSV 引用，未入库"
    if stats["notes"]:
        msg += "；" + "；".join(stats["notes"])
    return redirect("/", msg=msg)


@app.get("/licenses/{license_id}/files/{file_id}")
def download_license_file(license_id: int, file_id: int):
    """下载授权对应的扫描件原件（多条授权引用同一 PDF 时取同一份）。"""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT f.* FROM license_files lf JOIN files f ON f.id = lf.file_id"
            " JOIN licenses l ON l.id = lf.license_id"
            " WHERE lf.license_id=? AND lf.file_id=?", (license_id, file_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="该授权没有挂接此扫描件")
        path = storage.final_path(row["sha256"])
        if not os.path.isfile(path):
            raise HTTPException(status_code=410, detail="扫描件文件缺失，请重新导入材料包")
        filename = row["filename"] or f"{row['sha256'][:12]}.pdf"
    finally:
        conn.close()
    disposition = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
    return FileResponse(path, media_type="application/pdf",
                        headers={"Content-Disposition": disposition})


# ---------------------------------------------------------------- 撤回 / 确认 / 下载

@app.post("/licenses/{license_id}/revoke")
async def revoke(license_id: int):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        lic, message = logic.revoke_license(conn, license_id)
        if lic is None:
            conn.rollback()
            return redirect("/licenses", err=message)
        conn.commit()
    finally:
        conn.close()
    if lic["revoked"] and "此前已撤回" in message:
        return redirect("/licenses", err=message)
    return redirect("/licenses", msg=message)


@app.post("/exhibitions/{ex_id}/confirm")
async def confirm(ex_id: int, request: Request):
    form = await request.form()
    editor = str(form.get("editor", "")).strip()
    if not editor:
        return redirect(f"/exhibitions/{ex_id}", err="请填写编辑姓名")
    submitted = {}
    for key in form.keys():
        if key.startswith("license_"):
            try:
                oid = int(key[len("license_"):])
            except ValueError:
                continue
            submitted[oid] = (str(form.get(key, "")),
                              str(form.get(f"attribution_{oid}", "")))
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        get_exhibition_or_404(conn, ex_id)
        problems = logic.confirm_manifest(conn, ex_id, editor, submitted)
        if problems:
            conn.rollback()  # 维持停供
            return redirect(f"/exhibitions/{ex_id}", err="；".join(problems))
        conn.commit()
    finally:
        conn.close()
    return redirect(f"/exhibitions/{ex_id}", msg=f"编辑 {editor} 已确认清单，下载恢复")


@app.get("/exhibitions/{ex_id}/manifest.csv")
def download_manifest(ex_id: int):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        get_exhibition_or_404(conn, ex_id)
        st = logic.exhibition_status(conn, ex_id)
        if not st["downloadable"]:
            conn.rollback()
            return JSONResponse(status_code=409, content={
                "detail": "展图停供中：存在缺授权或署名未确认的原图",
                "missing": st["missing"],
                "unconfirmed": st["unconfirmed"],
            })
        items = st["items"]
        conn.commit()
    finally:
        conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["原图号", "授权号", "署名"])
    for it in items:
        writer.writerow([it["original_no"], it["license_no"], it["attribution"]])
    content = "﻿" + output.getvalue()  # BOM，便于 Excel 打开
    return Response(
        content, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="manifest_{ex_id}.csv"'})


# ---------------------------------------------------------------- JSON API

@app.get("/api/exhibitions")
def api_exhibitions():
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        logic.sync_all(conn)
        rows = conn.execute("SELECT * FROM exhibitions ORDER BY exhibition_no").fetchall()
        result = []
        for ex in rows:
            st = logic.exhibition_status(conn, ex["id"])
            result.append({
                "id": ex["id"], "exhibition_no": ex["exhibition_no"], "title": ex["title"],
                "downloadable": st["downloadable"],
                "missing": st["missing"], "unconfirmed": st["unconfirmed"],
            })
        conn.commit()
        return result
    finally:
        conn.close()


@app.get("/api/exhibitions/{ex_id}")
def api_exhibition(ex_id: int):
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ex = get_exhibition_or_404(conn, ex_id)
        st = logic.exhibition_status(conn, ex_id)
        conn.commit()
    finally:
        conn.close()
    return {
        "id": ex["id"], "exhibition_no": ex["exhibition_no"], "title": ex["title"],
        "downloadable": st["downloadable"],
        "missing": st["missing"], "unconfirmed": st["unconfirmed"],
        "items": [{
            "original_id": it["original_id"], "original_no": it["original_no"],
            "license_no": it["license_no"], "attribution": it["attribution"],
            "license_valid": it["license_valid"], "confirmed": bool(it["confirmed"]),
        } for it in st["items"]],
    }


@app.get("/api/licenses")
def api_licenses():
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        logic.sync_all(conn)
        rows = conn.execute(
            "SELECT l.id, l.license_no, o.original_no, l.commercial, l.attribution,"
            " l.revoked, l.revoked_at, l.batch_label,"
            " GROUP_CONCAT(DISTINCT e.exhibition_no) AS used_by"
            " FROM licenses l"
            " JOIN originals o ON o.id = l.original_id"
            " LEFT JOIN manifest_items mi ON mi.license_id = l.id"
            " LEFT JOIN exhibitions e ON e.id = mi.exhibition_id"
            " GROUP BY l.id ORDER BY l.id"
        ).fetchall()
        file_rows = conn.execute(
            "SELECT lf.license_id, f.id, f.filename, f.sha256, f.size"
            " FROM license_files lf JOIN files f ON f.id = lf.file_id"
            " ORDER BY lf.license_id, f.id").fetchall()
        conn.commit()
        by_license = {}
        for fr in file_rows:
            by_license.setdefault(fr["license_id"], []).append(dict(fr))
        result = []
        for r in rows:
            d = dict(r)
            d["files"] = by_license.get(d["id"], [])
            result.append(d)
        return result
    finally:
        conn.close()
