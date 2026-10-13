"""独立于原有单用户模拟盘的工作区持久化。"""

import os
from pathlib import Path
import sqlite3
import threading

import config


_SCHEMA_LOCK = threading.Lock()


def database_path() -> Path:
    configured = os.environ.get("ALPHAPILOT_WORKSPACE_DB", "").strip()
    return Path(configured or Path(config.DATA_DIR) / "workspace.sqlite3").expanduser()


def connect() -> sqlite3.Connection:
    """创建连接；调用方负责事务边界和关闭连接。"""
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    connection = sqlite3.connect(str(path), timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
    except Exception:
        connection.close()
        raise
    return connection


def init_db() -> None:
    """同进程与多进程启动均可安全重入，不触碰其他业务表。"""
    with _SCHEMA_LOCK:
        connection = connect()
        try:
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS workspace_users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    plan TEXT NOT NULL DEFAULT 'free',
                    email_verified INTEGER NOT NULL DEFAULT 0
                        CHECK (email_verified IN (0, 1)),
                    is_active INTEGER NOT NULL DEFAULT 1
                        CHECK (is_active IN (0, 1)),
                    credential_version INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_sessions (
                    token_hash TEXT PRIMARY KEY,
                    session_id TEXT,
                    user_id TEXT NOT NULL REFERENCES workspace_users(id)
                        ON DELETE CASCADE,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS workspace_sessions_user
                    ON workspace_sessions(user_id, created_at);
                CREATE INDEX IF NOT EXISTS workspace_sessions_expiry
                    ON workspace_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS workspace_auth_limits (
                    scope TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY (scope, key_hash)
                );
                CREATE INDEX IF NOT EXISTS workspace_auth_limits_expiry
                    ON workspace_auth_limits(expires_at);
                CREATE TABLE IF NOT EXISTS workspace_action_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES workspace_users(id) ON DELETE CASCADE,
                    purpose TEXT NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
                    credential_version INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER,
                    delivery_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (delivery_status IN ('pending', 'sent', 'failed', 'cancelled')),
                    delivery_error TEXT,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS workspace_action_tokens_user
                    ON workspace_action_tokens(user_id, purpose, created_at);
                CREATE INDEX IF NOT EXISTS workspace_action_tokens_expiry
                    ON workspace_action_tokens(expires_at);
            """)
            # 兼容首版已有数据库；持有 BEGIN IMMEDIATE 防止多个 worker 同时迁移。
            user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(workspace_users)")}
            if "credential_version" not in user_columns:
                connection.execute("ALTER TABLE workspace_users ADD COLUMN credential_version INTEGER NOT NULL DEFAULT 1")
            session_columns = {row["name"] for row in connection.execute("PRAGMA table_info(workspace_sessions)")}
            if "session_id" not in session_columns:
                connection.execute("ALTER TABLE workspace_sessions ADD COLUMN session_id TEXT")
            connection.execute("UPDATE workspace_sessions SET session_id = lower(hex(randomblob(18))) WHERE session_id IS NULL")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS workspace_sessions_public_id ON workspace_sessions(session_id)")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
