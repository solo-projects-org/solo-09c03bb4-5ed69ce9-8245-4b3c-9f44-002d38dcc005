"""扫描件存储：按 SHA-256 内容寻址去重，上传、落盘、下载全部按块处理。

存储目录默认与 SQLite 数据库同级的 ``files/``（Docker 中即 /data/files），
可用环境变量 ``EXHIBITION_FILES`` 覆盖。文件以
``pdf/<sha 前两位>/<sha>.pdf`` 保存；相同内容的多个 PDF 只保留一份。
"""
import hashlib
import os
import shutil
import tempfile
import time
from typing import BinaryIO, Tuple

from . import db

CHUNK_SIZE = 1024 * 1024  # 1 MiB


class Cancelled(Exception):
    """导入被取消（客户端断开/保存中断）；调用方应回滚且不留下任何数据。"""


def check_cancel(cancel) -> None:
    if cancel is not None and cancel():
        raise Cancelled()


def files_dir() -> str:
    override = os.environ.get("EXHIBITION_FILES")
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.dirname(os.path.abspath(db.db_path())), "files")


def stored_name(sha256: str) -> str:
    return f"pdf/{sha256[:2]}/{sha256}.pdf"


def final_path(sha256: str) -> str:
    return os.path.join(files_dir(), stored_name(sha256))


def _staging_root() -> str:
    return os.path.join(files_dir(), ".staging")


def new_staging_dir() -> str:
    """为一次材料包导入创建独立的暂存目录。"""
    root = _staging_root()
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(prefix="import-", dir=root)


def new_staging_file(suffix: str = ".zip") -> str:
    """创建一个暂存文件路径（用于接收上传的 ZIP，避免整包读入内存）。"""
    root = _staging_root()
    os.makedirs(root, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="upload-", suffix=suffix, dir=root)
    os.close(fd)
    return path


def max_pdf_bytes() -> int:
    """单个扫描件的解压后大小上限，默认 512 MiB。"""
    raw = os.environ.get("EXHIBITION_MAX_PDF_MB", "512")
    try:
        return int(float(raw) * 1024 * 1024)
    except ValueError:
        return 512 * 1024 * 1024


def max_package_bytes() -> int:
    """单个材料包解压后的总大小上限（防压缩炸弹），默认 4096 MiB。"""
    raw = os.environ.get("EXHIBITION_MAX_EXTRACT_MB", "4096")
    try:
        return int(float(raw) * 1024 * 1024)
    except ValueError:
        return 4096 * 1024 * 1024


def copy_stream_hashed(src: BinaryIO, dest_path: str,
                       chunk_size: int = CHUNK_SIZE,
                       max_bytes=None, cancel=None) -> Tuple[str, int]:
    """按块读取 ``src``，写入 ``dest_path``，同时计算 SHA-256 与字节数。

    失败时删除未写完的暂存文件。返回 ``(sha256, size)``。
    """
    digest = hashlib.sha256()
    total = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                check_cancel(cancel)
                chunk = src.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ValueError(
                        f"单个扫描件超过大小上限 {max_bytes // (1024 * 1024)} MiB")
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except Exception:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise
    return digest.hexdigest(), total


def publish(staged_path: str, sha256: str) -> str:
    """把已校验的暂存文件原子发布到内容寻址位置。

    同一内容重复导入时直接覆盖为相同字节，不会产生第二份文件。
    """
    dest = final_path(sha256)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.replace(staged_path, dest)
    return dest


def cleanup_stale(max_age_seconds: int = 24 * 3600) -> None:
    """启动时清理崩溃遗留的暂存目录/上传文件（默认超过 24 小时）。"""
    root = _staging_root()
    if not os.path.isdir(root):
        return
    now = time.time()
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if now - os.path.getmtime(path) <= max_age_seconds:
                continue
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except OSError:
            pass


def discard_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
