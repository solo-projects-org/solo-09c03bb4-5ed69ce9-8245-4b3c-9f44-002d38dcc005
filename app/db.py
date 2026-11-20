"""SQLite 连接与建表。所有状态持久化在 SQLite 中，服务重启后保持一致。"""
import os
import sqlite3

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "app.db")


def db_path() -> str:
    # 每次连接时读取环境变量，便于测试与 Docker 挂载覆盖
    return os.environ.get("EXHIBITION_DB", DEFAULT_DB)


def connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # isolation_level=None: 自动提交，由业务代码显式 BEGIN IMMEDIATE 控制写事务
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS exhibitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exhibition_no TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS originals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_no TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS exhibition_originals (
    exhibition_id INTEGER NOT NULL REFERENCES exhibitions(id),
    original_id INTEGER NOT NULL REFERENCES originals(id),
    PRIMARY KEY (exhibition_id, original_id)
);

CREATE TABLE IF NOT EXISTS licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_no TEXT NOT NULL UNIQUE,          -- 授权号唯一：重传不会覆盖，更不会恢复已撤回
    original_id INTEGER NOT NULL REFERENCES originals(id),
    commercial INTEGER NOT NULL DEFAULT 0,    -- 是否允许商用
    attribution TEXT NOT NULL DEFAULT '',     -- 署名
    revoked INTEGER NOT NULL DEFAULT 0,       -- 撤回为终态
    batch_label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS manifest_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exhibition_id INTEGER NOT NULL REFERENCES exhibitions(id),
    original_id INTEGER NOT NULL REFERENCES originals(id),
    license_id INTEGER REFERENCES licenses(id),   -- 当前选用的授权，NULL 表示缺授权
    attribution TEXT NOT NULL DEFAULT '',          -- 选用授权对应的署名
    confirmed INTEGER NOT NULL DEFAULT 0,          -- 编辑是否已确认该署名
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (exhibition_id, original_id)
);

CREATE TABLE IF NOT EXISTS confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exhibition_id INTEGER NOT NULL REFERENCES exhibitions(id),
    editor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 扫描件内容按 SHA-256 去重：同一份 PDF 被多条授权引用时只存一行/一份文件
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    filename TEXT NOT NULL DEFAULT '',        -- 最近一次导入时的原始文件名
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 授权与扫描件的挂接关系
CREATE TABLE IF NOT EXISTS license_files (
    license_id INTEGER NOT NULL REFERENCES licenses(id),
    file_id INTEGER NOT NULL REFERENCES files(id),
    linked_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (license_id, file_id)
);

-- 材料包导入记录：state=committed 才对外可见
CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_label TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,                       -- imported（文件已落盘，DB 未提交）/ committed
    pdf_count INTEGER NOT NULL DEFAULT 0,
    new_files INTEGER NOT NULL DEFAULT 0,
    new_licenses INTEGER NOT NULL DEFAULT 0,
    linked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
