"""ZIP 材料包导入的端到端自测：新授权、去重、补件、重传、撤回、失败原子性。"""
import csv
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

MAPPING_CSV = """展图号,展图名称,原图号
EXH-01,唐代壁画拼版,ORG-001
EXH-01,唐代壁画拼版,ORG-002
"""

# 旧入口（无 PDF 列）导入的授权，用于测试“补挂原件”
LEGACY_LICENSES_CSV = """授权号,原图号,是否允许商用,署名
LIC-001,ORG-001,是,摄影：张三
LIC-002,ORG-002,是,摄影：李四
"""

PDF_A = b"%PDF-1.4\n% scan A\n" + b"A" * 300000 + b"\n%%EOF\n"
PDF_B = b"%PDF-1.4\n% scan B\n" + b"B" * 200000 + b"\n%%EOF\n"
PDF_A2 = b"%PDF-1.4\n% scan A2\n" + b"Z" * 300000 + b"\n%%EOF\n"


def package_csv(rows) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["授权号", "原图号", "是否允许商用", "署名", "PDF文件名"])
    for r in rows:
        w.writerow(r)
    return out.getvalue()


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def upload_zip(client, files: dict):
    return client.post(
        "/upload/package",
        files={"file": ("package.zip", make_zip(files), "application/zip")},
        data={"batch": "test-batch"}, follow_redirects=False)


def flash(response, key="err"):
    """解析 303 跳转地址里的 msg/err 参数（与页面 flash 一致解码 +）。"""
    from urllib.parse import parse_qs, urlsplit
    qs = parse_qs(urlsplit(response.headers["location"]).query)
    return qs.get(key, [""])[0]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EXHIBITION_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXHIBITION_FILES", str(tmp_path / "files"))
    from app.main import app
    with TestClient(app) as c:
        yield c


def upload_csv(client, url, text, name="data.csv"):
    return client.post(url, files={"file": (name, text.encode("utf-8"), "text/csv")},
                       data={}, follow_redirects=False)


def setup_base(client):
    assert upload_csv(client, "/upload/mapping", MAPPING_CSV).status_code == 303
    assert upload_csv(client, "/upload/licenses", LEGACY_LICENSES_CSV).status_code == 303


def licenses(client):
    return {l["license_no"]: l for l in client.get("/api/licenses").json()}


def files_on_disk(tmp_path):
    root = tmp_path / "files"
    return sorted(p for p in root.rglob("*.pdf"))


def test_package_import_new_licenses_dedup_and_download(client):
    setup_base(client)
    csv_text = package_csv([
        ("LIC-101", "ORG-001", "是", "摄影：张三", "scan_a.pdf"),
        ("LIC-102", "ORG-001", "是", "摄影：张三", "scan_a.pdf"),   # 同一 PDF
        ("LIC-103", "ORG-002", "是", "摄影：李四", "scan_b.pdf"),
    ])
    r = upload_zip(client, {"licenses.csv": csv_text,
                            "scan_a.pdf": PDF_A, "scan_b.pdf": PDF_B})
    assert r.status_code == 303 and "msg=" in r.headers["location"], r.headers

    lics = licenses(client)
    assert lics["LIC-101"]["files"][0]["sha256"] == lics["LIC-102"]["files"][0]["sha256"]
    # 两条授权共用一份存储文件
    assert len({f["id"] for f in lics["LIC-101"]["files"]}) == 1
    assert lics["LIC-101"]["files"][0]["id"] == lics["LIC-102"]["files"][0]["id"]

    r = client.get(f"/licenses/{lics['LIC-101']['id']}/files/"
                   f"{lics['LIC-101']['files'][0]['id']}")
    assert r.status_code == 200
    assert r.content == PDF_A
    assert r.headers["content-type"] == "application/pdf"


def test_package_backfill_respects_commercial_and_revoked(client):
    setup_base(client)
    # 补挂旧授权原件：LIC-001/LIC-002 尚未挂原件
    csv_text = package_csv([
        ("LIC-001", "ORG-001", "是", "摄影：张三", "a.pdf"),
        ("LIC-002", "ORG-002", "是", "摄影：李四", "b.pdf"),
    ])
    r = upload_zip(client, {"licenses.csv": csv_text, "a.pdf": PDF_A, "b.pdf": PDF_B})
    assert "msg=" in r.headers["location"]
    lics = licenses(client)
    assert lics["LIC-001"]["files"][0]["filename"] == "a.pdf"
    assert lics["LIC-002"]["files"][0]["filename"] == "b.pdf"

    # 撤回 LIC-002 后再导入同号同内容：不恢复、不重新挂接
    client.post(f"/licenses/{lics['LIC-002']['id']}/revoke", follow_redirects=False)
    r = upload_zip(client, {"licenses.csv": package_csv([
        ("LIC-002", "ORG-002", "是", "摄影：李四", "b.pdf")]),
        "b.pdf": PDF_B})
    assert r.status_code == 303
    lics = licenses(client)
    assert lics["LIC-002"]["revoked"] == 1


def test_package_conflict_rejected_whole(client):
    setup_base(client)
    # 同号但署名不同 → 整包失败，且新授权也不应入库
    csv_text = package_csv([
        ("LIC-001", "ORG-001", "是", "摄影：别人", "a.pdf"),
        ("LIC-201", "ORG-002", "是", "摄影：李四", "b.pdf"),
    ])
    r = upload_zip(client, {"licenses.csv": csv_text, "a.pdf": PDF_A, "b.pdf": PDF_B})
    assert "署名不同" in flash(r)
    assert "LIC-201" not in licenses(client)
    lics = licenses(client)
    assert lics["LIC-001"]["files"] == []


def test_package_missing_file_rejected_and_retry_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("EXHIBITION_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXHIBITION_FILES", str(tmp_path / "files"))
    from app.main import app

    with TestClient(app) as c:
        setup_base(c)
        csv_text = package_csv([
            ("LIC-301", "ORG-001", "是", "摄影：张三", "a.pdf"),
            ("LIC-302", "ORG-002", "是", "摄影：李四", "missing.pdf"),
        ])
        r = upload_zip(c, {"licenses.csv": csv_text, "a.pdf": PDF_A})
        assert "缺件" in flash(r)
        assert "LIC-301" not in licenses(c)
        # 表单错误时的提示可见
        page = c.get("/")
        assert "缺件" in page.text or "未生效" in page.text
        assert files_on_disk(tmp_path) == []  # 失败包不留下扫描件

    # 重启后修正包（补上缺件）可正常重传
    with TestClient(app) as c:
        r = upload_zip(c, {"licenses.csv": csv_text,
                           "a.pdf": PDF_A, "missing.pdf": PDF_B})
        assert "msg=" in r.headers["location"]
        lics = licenses(c)
        assert lics["LIC-301"]["files"] and lics["LIC-302"]["files"]
        # 新授权与原图挂接后，展图清单可用
        assert c.get("/api/exhibitions/1").json()["items"]


def test_package_reupload_content_mismatch_and_exact_match(client):
    setup_base(client)
    csv_text = package_csv([("LIC-001", "ORG-001", "是", "摄影：张三", "a.pdf")])
    assert "msg=" in upload_zip(
        client, {"licenses.csv": csv_text, "a.pdf": PDF_A}).headers["location"]

    # 同号、同原图/商用/署名，但 PDF 内容不同 → 重传内容核对失败
    r = upload_zip(client, {"licenses.csv": csv_text, "a.pdf": PDF_A2})
    assert "内容" in flash(r)

    # 完全相同的包重传 → 允许（幂等）
    r = upload_zip(client, {"licenses.csv": csv_text, "a.pdf": PDF_A})
    assert "msg=" in r.headers["location"]


def test_package_duplicate_license_in_csv_rejected(client):
    setup_base(client)
    csv_text = package_csv([
        ("LIC-401", "ORG-001", "是", "摄影：张三", "a.pdf"),
        ("LIC-401", "ORG-002", "是", "摄影：李四", "b.pdf"),
    ])
    r = upload_zip(client, {"licenses.csv": csv_text, "a.pdf": PDF_A, "b.pdf": PDF_B})
    assert "重复" in flash(r)
    assert "LIC-401" not in licenses(client)


def test_package_manifest_only_after_new_license_and_file(client):
    """包内新授权与原件一起入库后，才可用于展图清单。"""
    setup_base(client)
    # 先确认旧授权，展图 1 可下载
    st = client.get("/api/exhibitions/1").json()
    data = {"editor": "王编辑"}
    for it in st["items"]:
        if it["license_valid"]:
            data[f"license_{it['original_id']}"] = it["license_no"]
            data[f"attribution_{it['original_id']}"] = it["attribution"]
    client.post("/exhibitions/1/confirm", data=data, follow_redirects=False)
    assert client.get("/api/exhibitions/1").json()["downloadable"] is True

    # 导入含新授权的新包（同署名）
    csv_text = package_csv([("LIC-501", "ORG-001", "是", "摄影：张三", "a.pdf")])
    assert "msg=" in upload_zip(
        client, {"licenses.csv": csv_text, "a.pdf": PDF_A}).headers["location"]
    lics = licenses(client)
    assert lics["LIC-501"]["files"]


def test_package_non_pdf_and_bad_zip(client):
    setup_base(client)
    r = upload_zip(client, {"licenses.csv": package_csv(
        [("LIC-601", "ORG-001", "是", "摄影：张三", "a.pdf")]),
        "a.pdf": b"not a pdf"})
    assert "不是 PDF" in flash(r)

    r = client.post("/upload/package",
                    files={"file": ("x.zip", b"nope", "application/zip")},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_failed_import_keeps_preexisting_file(client, tmp_path):
    """失败回滚时不得删除此前已入库的同内容扫描件。"""
    setup_base(client)
    good = package_csv([("LIC-701", "ORG-001", "是", "摄影：张三", "a.pdf")])
    assert "msg=" in upload_zip(
        client, {"licenses.csv": good, "a.pdf": PDF_A}).headers["location"]
    before = files_on_disk(tmp_path)
    assert len(before) == 1

    # 第二个包复用 a.pdf，同时引用缺件 → 整包失败，a.pdf 必须保留
    bad = package_csv([
        ("LIC-702", "ORG-001", "是", "摄影：张三", "a.pdf"),
        ("LIC-703", "ORG-002", "是", "摄影：李四", "gone.pdf"),
    ])
    assert "缺件" in flash(upload_zip(client, {"licenses.csv": bad, "a.pdf": PDF_A}))
    assert files_on_disk(tmp_path) == before
    lics = licenses(client)
    assert "LIC-702" not in lics
    r = client.get(f"/licenses/{lics['LIC-701']['id']}/files/"
                   f"{lics['LIC-701']['files'][0]['id']}")
    assert r.status_code == 200 and r.content == PDF_A


def test_package_multiple_pdfs_per_license_content_check(client):
    """已挂多份原件的授权：重传时全部内容相同才通过，任一不同即失败。"""
    setup_base(client)
    csv_text = package_csv([
        ("LIC-001", "ORG-001", "是", "摄影：张三", "a.pdf"),
        ("LIC-001", "ORG-001", "是", "摄影：张三", "b.pdf"),
    ])
    r = upload_zip(client, {"licenses.csv": csv_text, "a.pdf": PDF_A, "b.pdf": PDF_B})
    assert "msg=" in r.headers["location"], flash(r)
    lics = licenses(client)
    assert len(lics["LIC-001"]["files"]) == 2

    # 其中一份换成不同内容 → 重传核对失败
    r = upload_zip(client, {"licenses.csv": csv_text, "a.pdf": PDF_A, "b.pdf": PDF_A2})
    assert "内容" in flash(r)
    # 两份都与已挂内容完全相同 → 通过
    r = upload_zip(client, {"licenses.csv": csv_text, "a.pdf": PDF_A, "b.pdf": PDF_B})
    assert "msg=" in r.headers["location"]


def test_package_same_content_two_names_one_stored_copy(client, tmp_path):
    """包内两个不同文件名、内容相同的 PDF 只保存一份。"""
    setup_base(client)
    csv_text = package_csv([
        ("LIC-801", "ORG-001", "是", "摄影：张三", "a.pdf"),
        ("LIC-802", "ORG-002", "是", "摄影：李四", "b.pdf"),
    ])
    r = upload_zip(client, {"licenses.csv": csv_text, "a.pdf": PDF_A, "b.pdf": PDF_A})
    assert "msg=" in r.headers["location"], flash(r)
    assert len(files_on_disk(tmp_path)) == 1
    lics = licenses(client)
    assert lics["LIC-801"]["files"][0]["id"] == lics["LIC-802"]["files"][0]["id"]


def test_package_licenses_page_shows_download_link(client):
    setup_base(client)
    csv_text = package_csv([("LIC-901", "ORG-001", "是", "摄影：张三", "a.pdf")])
    assert "msg=" in upload_zip(
        client, {"licenses.csv": csv_text, "a.pdf": PDF_A}).headers["location"]
    page = client.get("/licenses")
    assert page.status_code == 200
    lics = licenses(client)
    assert f"/licenses/{lics['LIC-901']['id']}/files/{lics['LIC-901']['files'][0]['id']}" in page.text


def test_package_conflict_on_commercial_flag(client):
    setup_base(client)
    csv_text = package_csv([("LIC-001", "ORG-001", "否", "摄影：张三", "a.pdf")])
    assert "商用标记不同" in flash(upload_zip(
        client, {"licenses.csv": csv_text, "a.pdf": PDF_A}))
    assert licenses(client)["LIC-001"]["commercial"] == 1


def test_package_pdf_name_with_folder_prefix(client):
    """CSV 里的 PDF 文件名可带目录前缀，按文件名绑定包内原件。"""
    setup_base(client)
    csv_text = package_csv([("LIC-011", "ORG-001", "是", "摄影：张三", "scans/扫描 01.pdf")])
    r = upload_zip(client, {"licenses.csv": csv_text, "scans/扫描 01.pdf": PDF_A})
    assert "msg=" in r.headers["location"], flash(r)
    lics = licenses(client)
    f = lics["LIC-011"]["files"][0]
    assert f["filename"] == "扫描 01.pdf"
    assert client.get(f"/licenses/{lics['LIC-011']['id']}/files/{f['id']}").content == PDF_A


def test_package_missing_pdf_column(client):
    setup_base(client)
    csv_text = "授权号,原图号,是否允许商用,署名\nLIC-021,ORG-001,是,摄影：张三\n"
    assert "PDF文件名" in flash(upload_zip(
        client, {"licenses.csv": csv_text, "a.pdf": PDF_A}))


def test_save_interrupted_rolls_back_and_allows_retry(tmp_path, monkeypatch):
    """保存中断（提交前抛错）：整包不生效、不留扫描件；重启后可重传。"""
    monkeypatch.setenv("EXHIBITION_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXHIBITION_FILES", str(tmp_path / "files"))
    from app.main import app
    from app import package_import

    csv_text = package_csv([("LIC-031", "ORG-001", "是", "摄影：张三", "a.pdf")])
    real_commit = package_import.mark_batch_committed

    def boom(*a, **kw):
        raise OSError("simulated disk failure")

    with TestClient(app, raise_server_exceptions=False) as c:
        setup_base(c)
        monkeypatch.setattr(package_import, "mark_batch_committed", boom)
        r = upload_zip(c, {"licenses.csv": csv_text, "a.pdf": PDF_A})
        assert "保存中断" in flash(r)
        assert "LIC-031" not in licenses(c)

    # 重启（新进程语义）：失败包未入库、未留下文件
    with TestClient(app, raise_server_exceptions=False) as c:
        assert "LIC-031" not in licenses(c)
        assert files_on_disk(tmp_path) == []
        monkeypatch.setattr(package_import, "mark_batch_committed", real_commit)
        r = upload_zip(c, {"licenses.csv": csv_text, "a.pdf": PDF_A})
        assert "msg=" in r.headers["location"], flash(r)
        lics = licenses(c)
        assert lics["LIC-031"]["files"]


def test_package_size_caps(client, monkeypatch):
    setup_base(client)
    csv_text = package_csv([("LIC-051", "ORG-001", "是", "摄影：张三", "a.pdf")])
    monkeypatch.setenv("EXHIBITION_MAX_PDF_MB", "0.05")  # 约 52 KiB
    assert "上限" in flash(upload_zip(
        client, {"licenses.csv": csv_text, "a.pdf": PDF_A}))
    monkeypatch.setenv("EXHIBITION_MAX_PDF_MB", "512")
    monkeypatch.setenv("EXHIBITION_MAX_EXTRACT_MB", "0.05")
    assert "上限" in flash(upload_zip(
        client, {"licenses.csv": csv_text, "a.pdf": PDF_A}))
    assert "LIC-051" not in licenses(client)


def test_shipped_sample_package_imports(client):
    """仓库自带的示例材料包可直接导入。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "sample_data"
    data = (root / "licenses_package.zip").read_bytes()
    r = client.post("/upload/package",
                    files={"file": ("licenses_package.zip", data, "application/zip")},
                    follow_redirects=False)
    assert "msg=" in r.headers["location"], flash(r)
    lics = licenses(client)
    # 同一 PDF 被多条授权引用 → 共用一份存储文件
    assert lics["LIC-001"]["files"][0]["id"] == lics["LIC-002"]["files"][0]["id"]
