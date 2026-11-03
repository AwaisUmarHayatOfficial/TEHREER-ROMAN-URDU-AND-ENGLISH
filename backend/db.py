"""MySQL access (PyMySQL) with a tiny connection pool + schema bootstrap from tehreer.sql."""
import logging
import os
import queue
import re
import time
from contextlib import contextmanager
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

log = logging.getLogger("tehreer.db")

BASE = Path(__file__).resolve().parent
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "tehreer")
DB_USER = os.getenv("DB_USER", "tehreer")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "8"))
DB_WAIT_SECONDS = int(os.getenv("DB_WAIT_SECONDS", "90"))
SCHEMA_FILE = Path(os.getenv("SCHEMA_FILE", BASE.parent / "tehreer.sql"))

IntegrityError = pymysql.err.IntegrityError
_pool: "queue.LifoQueue" = queue.LifoQueue()


def _connect():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
        charset="utf8mb4", cursorclass=DictCursor, autocommit=True,
        connect_timeout=10, read_timeout=30, write_timeout=30,
    )


@contextmanager
def connection():
    try:
        conn = _pool.get_nowait()
        conn.ping(reconnect=True)
    except queue.Empty:
        conn = _connect()
    ok = True
    try:
        yield conn
    except Exception:
        ok = False
        raise
    finally:
        if ok and _pool.qsize() < POOL_SIZE:
            _pool.put(conn)
        else:
            try:
                conn.close()
            except Exception:
                pass


def query(sql: str, params=()) -> list:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def one(sql: str, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()):
    """Returns (lastrowid, rowcount)."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.lastrowid, cur.rowcount


def _statements(text: str):
    """Splits an SQL script on ';' (semicolons inside quoted strings are kept)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
    buf, quote = [], None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                yield stmt
            buf = []
        else:
            buf.append(ch)
    stmt = "".join(buf).strip()
    if stmt:
        yield stmt


# Columns / indexes added after the first release. Applied only when missing, so existing
# databases (created from an older tehreer.sql) are upgraded in place without losing data.
_COLUMNS = [
    ("users", "first_name", "ALTER TABLE `users` ADD COLUMN `first_name` VARCHAR(60) NULL AFTER `email_normalized`"),
    ("users", "last_name", "ALTER TABLE `users` ADD COLUMN `last_name` VARCHAR(60) NULL AFTER `first_name`"),
    ("users", "username", "ALTER TABLE `users` ADD COLUMN `username` VARCHAR(30) NULL AFTER `last_name`"),
    ("users", "phone", "ALTER TABLE `users` ADD COLUMN `phone` VARCHAR(20) NULL AFTER `username`"),
    ("users", "password_hash", "ALTER TABLE `users` ADD COLUMN `password_hash` VARCHAR(255) NULL"),
    ("users", "google_sub", "ALTER TABLE `users` ADD COLUMN `google_sub` VARCHAR(64) NULL"),
    ("users", "avatar_url", "ALTER TABLE `users` ADD COLUMN `avatar_url` VARCHAR(500) NULL"),
    ("users", "email_verified_at", "ALTER TABLE `users` ADD COLUMN `email_verified_at` DATETIME NULL"),
    ("users", "failed_logins", "ALTER TABLE `users` ADD COLUMN `failed_logins` SMALLINT UNSIGNED NOT NULL DEFAULT 0"),
    ("users", "locked_until", "ALTER TABLE `users` ADD COLUMN `locked_until` DATETIME NULL"),
    ("users", "password_changed_at", "ALTER TABLE `users` ADD COLUMN `password_changed_at` DATETIME NULL"),
    ("jobs", "language", "ALTER TABLE `jobs` ADD COLUMN `language` VARCHAR(20) NOT NULL DEFAULT 'roman_urdu' AFTER `video_url`"),
    ("login_codes", "purpose", "ALTER TABLE `login_codes` ADD COLUMN `purpose` VARCHAR(20) NOT NULL DEFAULT 'verify_email' AFTER `email_normalized`"),
]
_INDEXES = [
    ("users", "uq_users_username", "ALTER TABLE `users` ADD UNIQUE KEY `uq_users_username` (`username`)"),
    ("users", "uq_users_google_sub", "ALTER TABLE `users` ADD UNIQUE KEY `uq_users_google_sub` (`google_sub`)"),
]


def migrate():
    for table, col, ddl in _COLUMNS:
        if not one("SELECT 1 AS x FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                   "AND TABLE_NAME = %s AND COLUMN_NAME = %s", (table, col)):
            log.info("Migration: adding %s.%s", table, col)
            execute(ddl)
    for table, idx, ddl in _INDEXES:
        if not one("SELECT 1 AS x FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
                   "AND TABLE_NAME = %s AND INDEX_NAME = %s", (table, idx)):
            log.info("Migration: adding index %s", idx)
            execute(ddl)
    col = one("SELECT COLUMN_TYPE AS t FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
              "AND TABLE_NAME = 'users' AND COLUMN_NAME = 'status'")
    if col and "pending" not in col["t"]:
        log.info("Migration: extending users.status")
        execute("ALTER TABLE `users` MODIFY COLUMN `status` ENUM('pending','active','blocked') NOT NULL DEFAULT 'active'")
    # users created by the earlier passwordless version already proved their address
    execute("UPDATE `users` SET email_verified_at = COALESCE(email_verified_at, created_at) "
            "WHERE status = 'active' AND email_verified_at IS NULL")


def init_schema():
    """Wait for MySQL, apply tehreer.sql (tables first, then in-place migrations, then views).
    CREATE DATABASE / USE are skipped: the database already exists."""
    deadline = time.time() + DB_WAIT_SECONDS
    last = None
    while True:
        try:
            with connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
            break
        except Exception as e:  # database still starting
            last = e
            if time.time() > deadline:
                raise RuntimeError(f"Cannot connect to MySQL at {DB_HOST}:{DB_PORT} ({last})")
            log.info("Waiting for MySQL (%s)...", type(e).__name__)
            time.sleep(2)
    if not SCHEMA_FILE.exists():
        log.warning("Schema file %s not found; skipping schema bootstrap", SCHEMA_FILE)
        return
    tables, views = [], []
    for stmt in _statements(SCHEMA_FILE.read_text(encoding="utf-8")):
        head = stmt.upper()
        if head.startswith("CREATE DATABASE") or head.startswith("USE "):
            continue
        (views if head.startswith("CREATE OR REPLACE") else tables).append(stmt)
    for stmt in tables:
        execute(stmt)
    migrate()
    for stmt in views:
        execute(stmt)
    log.info("Database schema ready (%d tables, %d views)", len(tables), len(views))
