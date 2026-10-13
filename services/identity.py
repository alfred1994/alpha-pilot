"""工作区身份、一次性邮件证明、可撤销会话与免费套餐权限。"""

import base64
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
import time

from services import workspace_db, workspace_mail


SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_SESSIONS_PER_USER = 10
WORKSPACE_READ_LIMIT = 120
WORKSPACE_WRITE_LIMIT = 30
WORKSPACE_LIMIT_WINDOW_SECONDS = 60
EMAIL_VERIFICATION_TTL_SECONDS = 24 * 60 * 60
PASSWORD_RESET_TTL_SECONDS = 30 * 60
MAIL_PENDING_TIMEOUT_SECONDS = 120
_LOG = logging.getLogger(__name__)
SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 1
_PASSWORD_SLOTS = threading.BoundedSemaphore(2)
_EMAIL_LOCAL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+\Z", re.ASCII)
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)
_COMMON_PASSWORDS = {
    "password1234", "password12345", "password123456", "passwordpassword",
    "qwerty123456", "qwertyuiop123", "qwertyuiopasdf", "administrator",
    "admin12345678", "letmein123456", "welcome123456", "iloveyou12345",
    "abc123abc123", "abcdefabcdef", "abcdefghijkl", "123456abcdef",
}


class IdentityError(Exception):
    def __init__(self, status_code: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class Session:
    user: dict
    token: str
    expires_at: int


@dataclass(frozen=True)
class ActionDelivery:
    """仅在当前投递任务内存中持有原始令牌，repr 和数据库均不保存它。"""
    token: str = field(repr=False)
    recipient: str = field(repr=False)
    purpose: str


def get_entitlements(plan: str = "free") -> dict:
    # 升级套餐必须由未来经过验证的计费事件授予；未知套餐也不能扩大权限。
    return {
        "accounts": 3,
        "accounts_per_market": 1,
        "markets": ["CN", "US", "CRYPTO"],
        "mode": "paper",
    }


def registration_allowed() -> bool:
    return os.environ.get("ALPHAPILOT_ALLOW_REGISTRATION", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def require_verified_email() -> bool:
    configured = os.environ.get("ALPHAPILOT_REQUIRE_VERIFIED_EMAIL")
    if configured is not None:
        return configured.strip().lower() not in {"0", "false", "no", "off"}
    return (
        os.environ.get("ALPHAPILOT_ENV", "").lower() == "production"
        or os.environ.get("ENV", "").lower() == "production"
        or os.environ.get("PRODUCTION", "").lower() in {"true", "1"}
    )


def assert_email_verified(user) -> None:
    if require_verified_email() and not user["email_verified"]:
        raise IdentityError(403, "请先验证邮箱，再创建账户或进行模拟交易")


def capabilities() -> dict:
    return {
        "email_delivery_available": workspace_mail.available(),
        "registration_allowed": registration_allowed(),
        "require_verified_email": require_verified_email(),
    }


def normalize_email(email: str) -> str:
    if not isinstance(email, str) or len(email) > 254:
        raise IdentityError(422, "请输入有效的邮箱地址")
    value = email.strip().casefold()
    if value.count("@") != 1:
        raise IdentityError(422, "请输入有效的邮箱地址")
    local, domain = value.split("@")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        raise IdentityError(422, "请输入有效的邮箱地址") from None
    labels = domain.split(".")
    if (
        not local or len(local) > 64 or not _EMAIL_LOCAL.fullmatch(local)
        or local.startswith(".") or local.endswith(".") or ".." in local
        or len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)
        or len(labels[-1]) < 2 or labels[-1].isdigit()
        or len(local) + len(domain) + 1 > 254
    ):
        raise IdentityError(422, "请输入有效的邮箱地址")
    return f"{local}@{domain}"


def validate_password(password: str, email: str) -> None:
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise IdentityError(422, "密码须为 12 至 128 个字符，建议使用较长的独特短语")
    if any(ord(character) < 32 or ord(character) == 127 or 0xD800 <= ord(character) <= 0xDFFF for character in password):
        raise IdentityError(422, "密码不能包含控制字符")
    compact = re.sub(r"[\W_]", "", password.casefold())
    if (
        len(set(password)) < 4 or password.isdigit()
        or compact in _COMMON_PASSWORDS
        or compact == re.sub(r"[\W_]", "", email.split("@")[0])
    ):
        raise IdentityError(422, "这个密码过于常见，请使用较长的独特短语")


def _derive_password(password: str, salt: bytes) -> bytes:
    # 限制 KDF 并发内存，避免同时注册或暴力尝试耗尽 Web 进程内存。
    if not _PASSWORD_SLOTS.acquire(timeout=2):
        raise IdentityError(429, "认证服务繁忙，请稍后再试", 2)
    try:
        return hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
            dklen=64, maxmem=64 * 1024 * 1024,
        )
    finally:
        _PASSWORD_SLOTS.release()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _derive_password(password, salt)
    return "$".join([
        "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ])


def verify_password(password: str, encoded: str | None) -> bool:
    try:
        algorithm, n, r, p, salt_text, digest_text = (encoded or "").split("$")
        if (algorithm, n, r, p) != ("scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P)):
            raise ValueError("unsupported password format")
        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(digest_text, validate=True)
        if len(salt) != 16 or len(expected) != 64:
            raise ValueError("invalid password format")
    except (ValueError, TypeError):
        # 未注册邮箱也执行同等成本的 KDF，避免通过明显的耗时差枚举账号。
        _derive_password(password, bytes(16))
        return False
    return hmac.compare_digest(_derive_password(password, salt), expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _csrf_token(token: str) -> str:
    # 根据只有浏览器和本次请求知道的会话秘密生成；数据库不保存任何可用会话令牌。
    return hmac.new(token.encode("ascii"), b"AlphaPilot workspace CSRF v1", hashlib.sha256).hexdigest()


def _user_payload(row: sqlite3.Row, token: str, session_id: str | None = None) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "plan": row["plan"],
        "email_verified": bool(row["email_verified"]),
        "csrf_token": _csrf_token(token),
        "session_id": session_id if session_id is not None else row["session_id"],
    }


def _consume_limits(limits: list[tuple[str, str, int, int]], message: str) -> None:
    """在 SQLite 中原子计数，重启与多 worker 都不会重置限速。"""
    now = int(time.time())
    with closing(workspace_db.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM workspace_auth_limits WHERE expires_at <= ?", (now,))
        updates = []
        for scope, key, maximum, window in limits:
            key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
            row = connection.execute(
                "SELECT attempts, expires_at FROM workspace_auth_limits WHERE scope = ? AND key_hash = ?",
                (scope, key_hash),
            ).fetchone()
            if row and row["attempts"] >= maximum:
                raise IdentityError(429, message, max(1, row["expires_at"] - now))
            updates.append((scope, key_hash, 1 if row is None else row["attempts"] + 1,
                            now + window if row is None else row["expires_at"]))
        connection.executemany(
            """INSERT INTO workspace_auth_limits (scope, key_hash, attempts, expires_at)
               VALUES (?, ?, ?, ?) ON CONFLICT(scope, key_hash) DO UPDATE
               SET attempts = excluded.attempts, expires_at = excluded.expires_at""",
            updates,
        )


def _consume_attempt(action: str, client_ip: str, email: str) -> None:
    if action == "register":
        limits = [("register_ip", client_ip, 5, 3600), ("register_email", email, 5, 900)]
    else:
        limits = [("login_ip", client_ip, 30, 900), ("login_email", email, 12, 900)]
    _consume_limits(limits, "尝试过于频繁，请稍后再试")


def consume_workspace_request(user_id: str, action: str) -> None:
    if action not in {"read", "write"}:
        raise ValueError("unsupported workspace request action")
    workspace_db.init_db()
    maximum = WORKSPACE_READ_LIMIT if action == "read" else WORKSPACE_WRITE_LIMIT
    _consume_limits(
        [(f"workspace_{action}", user_id, maximum, WORKSPACE_LIMIT_WINDOW_SECONDS)],
        "工作区请求过于频繁，请稍后再试",
    )


def _new_session(connection: sqlite3.Connection, row: sqlite3.Row, now: int) -> Session:
    token = secrets.token_urlsafe(32)
    session_id = secrets.token_urlsafe(18)
    expires_at = now + SESSION_TTL_SECONDS
    connection.execute("DELETE FROM workspace_sessions WHERE expires_at <= ?", (now,))
    connection.execute(
        "INSERT INTO workspace_sessions (token_hash, session_id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (_token_hash(token), session_id, row["id"], now, expires_at),
    )
    connection.execute(
        """DELETE FROM workspace_sessions WHERE user_id = ? AND token_hash NOT IN
           (SELECT token_hash FROM workspace_sessions WHERE user_id = ?
            ORDER BY created_at DESC, rowid DESC LIMIT ?)""",
        (row["id"], row["id"], MAX_SESSIONS_PER_USER),
    )
    return Session(_user_payload(row, token, session_id), token, expires_at)


def register(email: str, password: str, client_ip: str) -> Session:
    if not registration_allowed():
        raise IdentityError(403, "当前暂未开放新用户注册")
    email = normalize_email(email)
    workspace_db.init_db()
    _consume_attempt("register", client_ip, email)
    validate_password(password, email)
    encoded = hash_password(password)
    now = int(time.time())
    try:
        with closing(workspace_db.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            user_id = secrets.token_urlsafe(18)
            connection.execute(
                """INSERT INTO workspace_users
                   (id, email, password_hash, plan, email_verified, created_at, updated_at)
                   VALUES (?, ?, ?, 'free', 0, ?, ?)""",
                (user_id, email, encoded, now, now),
            )
            row = connection.execute("SELECT * FROM workspace_users WHERE id = ?", (user_id,)).fetchone()
            return _new_session(connection, row, now)
    except sqlite3.IntegrityError:
        raise IdentityError(409, "这个邮箱已注册，请登录") from None


def login(email: str, password: str, client_ip: str) -> Session:
    email = normalize_email(email)
    if (
        not isinstance(password, str) or not 1 <= len(password) <= 128
        or any(0xD800 <= ord(character) <= 0xDFFF for character in password)
    ):
        raise IdentityError(422, "密码长度不正确")
    workspace_db.init_db()
    _consume_attempt("login", client_ip, email)
    with closing(workspace_db.connect()) as connection:
        row = connection.execute("SELECT * FROM workspace_users WHERE email = ?", (email,)).fetchone()
    valid = verify_password(password, row["password_hash"] if row else None)
    if not valid or row is None or not row["is_active"]:
        raise IdentityError(401, "邮箱或密码不正确")
    with closing(workspace_db.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        # 管理员在慢速密码校验期间禁用账号时，也不能再签发会话。
        current = connection.execute("SELECT * FROM workspace_users WHERE id = ?", (row["id"],)).fetchone()
        if current is None or not current["is_active"] or current["password_hash"] != row["password_hash"]:
            raise IdentityError(401, "邮箱或密码不正确")
        return _new_session(connection, current, int(time.time()))


def get_session(token: str | None) -> dict | None:
    if not token or len(token) != 43 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return None
    workspace_db.init_db()
    with closing(workspace_db.connect()) as connection:
        row = connection.execute(
            """SELECT u.*, s.session_id FROM workspace_users u JOIN workspace_sessions s ON s.user_id = u.id
               WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1""",
            (_token_hash(token), int(time.time())),
        ).fetchone()
    return _user_payload(row, token) if row else None


def revoke_session(token: str | None) -> None:
    if not token or len(token) != 43 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return
    workspace_db.init_db()
    with closing(workspace_db.connect()) as connection, connection:
        connection.execute("DELETE FROM workspace_sessions WHERE token_hash = ?", (_token_hash(token),))


def _iso_timestamp(value: int | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


def _valid_token(token) -> bool:
    return isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is not None


def _active_session(connection: sqlite3.Connection, user_id: str, token: str):
    if not _valid_token(token):
        raise IdentityError(401, "请重新登录工作区")
    row = connection.execute(
        """SELECT u.*, s.session_id FROM workspace_users u
           JOIN workspace_sessions s ON s.user_id=u.id
           WHERE u.id=? AND u.is_active=1 AND s.token_hash=? AND s.expires_at>?""",
        (user_id, _token_hash(token), int(time.time())),
    ).fetchone()
    if row is None:
        raise IdentityError(401, "请重新登录工作区")
    return row


def list_sessions(user_id: str, current_token: str) -> list[dict]:
    workspace_db.init_db()
    with closing(workspace_db.connect()) as connection:
        connection.execute("BEGIN")
        current = _active_session(connection, user_id, current_token)
        rows = connection.execute(
            "SELECT session_id, created_at, expires_at FROM workspace_sessions WHERE user_id=? AND expires_at>? ORDER BY created_at DESC, rowid DESC",
            (user_id, int(time.time())),
        ).fetchall()
    return [{"id": row["session_id"], "created_at": _iso_timestamp(row["created_at"]),
             "expires_at": _iso_timestamp(row["expires_at"]), "current": row["session_id"] == current["session_id"]}
            for row in rows]


def revoke_session_by_id(user_id: str, current_token: str, session_id: str) -> bool:
    workspace_db.init_db()
    with closing(workspace_db.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        current = _active_session(connection, user_id, current_token)
        deleted = connection.execute("DELETE FROM workspace_sessions WHERE user_id=? AND session_id=?", (user_id, session_id))
        if deleted.rowcount != 1:
            raise IdentityError(404, "会话不存在或已经退出")
    return current["session_id"] == session_id


def revoke_other_sessions(user_id: str, current_token: str) -> None:
    workspace_db.init_db()
    with closing(workspace_db.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        current = _active_session(connection, user_id, current_token)
        connection.execute("DELETE FROM workspace_sessions WHERE user_id=? AND session_id<>?", (user_id, current["session_id"]))


def _require_mail() -> None:
    try:
        workspace_mail.configuration()
    except workspace_mail.MailError:
        raise IdentityError(503, "邮件服务暂不可用，请稍后重试或联系管理员") from None


def _prepare_action(connection: sqlite3.Connection, user, purpose: str, now: int) -> ActionDelivery:
    token = secrets.token_urlsafe(32)
    ttl = EMAIL_VERIFICATION_TTL_SECONDS if purpose == "verify_email" else PASSWORD_RESET_TTL_SECONDS
    # 仅保留最近 30 天的脱敏投递记录；最新邮件才具有操作权限。
    connection.execute("DELETE FROM workspace_action_tokens WHERE expires_at<?", (now - 30 * 86400,))
    connection.execute(
        "UPDATE workspace_action_tokens SET consumed_at=?, updated_at=? WHERE user_id=? AND purpose=? AND consumed_at IS NULL",
        (now, now, user["id"], purpose),
    )
    connection.execute(
        """INSERT INTO workspace_action_tokens
           (token_hash,user_id,purpose,credential_version,created_at,expires_at,updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (_token_hash(token), user["id"], purpose, user["credential_version"], now, now + ttl, now),
    )
    return ActionDelivery(token, user["email"], purpose)


def _deliver_action(delivery: ActionDelivery) -> None:
    digest = _token_hash(delivery.token)
    now = int(time.time())
    with closing(workspace_db.connect()) as connection:
        row = connection.execute(
            """SELECT t.* FROM workspace_action_tokens t JOIN workspace_users u ON u.id=t.user_id
               WHERE t.token_hash=? AND t.purpose=? AND t.delivery_status='pending'
               AND t.consumed_at IS NULL AND t.expires_at>? AND t.created_at>?
               AND u.is_active=1 AND u.email=? AND u.credential_version=t.credential_version""",
            (digest, delivery.purpose, now, now - MAIL_PENDING_TIMEOUT_SECONDS, delivery.recipient),
        ).fetchone()
    if row is None:
        with closing(workspace_db.connect()) as connection, connection:
            connection.execute(
                "UPDATE workspace_action_tokens SET delivery_status='cancelled', delivery_error='no_longer_eligible', consumed_at=COALESCE(consumed_at,?), updated_at=? WHERE token_hash=? AND delivery_status='pending'",
                (now, now, digest),
            )
        raise IdentityError(422, "本次邮件请求已失效，请重新申请")
    try:
        workspace_mail.send_action_email(delivery.recipient, delivery.purpose, delivery.token)
    except workspace_mail.MailError:
        with closing(workspace_db.connect()) as connection, connection:
            connection.execute(
                "UPDATE workspace_action_tokens SET delivery_status='failed', delivery_error='delivery_failed', consumed_at=COALESCE(consumed_at,?), updated_at=? WHERE token_hash=?",
                (int(time.time()), int(time.time()), digest),
            )
        _LOG.warning("Workspace account email delivery failed (%s); retry required", delivery.purpose)
        raise IdentityError(503, "邮件暂时无法投递，请稍后重试") from None
    with closing(workspace_db.connect()) as connection, connection:
        changed = connection.execute(
            """UPDATE workspace_action_tokens SET delivery_status='sent', updated_at=?
               WHERE token_hash=? AND delivery_status='pending' AND consumed_at IS NULL
               AND EXISTS (SELECT 1 FROM workspace_users u WHERE u.id=workspace_action_tokens.user_id
                           AND u.is_active=1 AND u.credential_version=workspace_action_tokens.credential_version)""",
            (int(time.time()), digest),
        )
        if changed.rowcount != 1:
            raise IdentityError(422, "本次邮件请求已失效，请重新申请")


def request_email_verification(user_id: str, current_token: str, client_ip: str) -> None:
    workspace_db.init_db()
    _consume_limits([("verify_request_user", user_id, 3, 900), ("verify_request_ip", client_ip, 10, 900)], "验证邮件请求过于频繁，请稍后重试")
    _require_mail()
    with closing(workspace_db.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        user = _active_session(connection, user_id, current_token)
        if user["email_verified"]:
            raise IdentityError(409, "邮箱已经验证，无需重复申请")
        delivery = _prepare_action(connection, user, "verify_email", int(time.time()))
    _deliver_action(delivery)


def email_verification_status(user_id: str, current_token: str) -> dict:
    workspace_db.init_db()
    with closing(workspace_db.connect()) as connection:
        connection.execute("BEGIN")
        user = _active_session(connection, user_id, current_token)
        latest = connection.execute(
            "SELECT * FROM workspace_action_tokens WHERE user_id=? AND purpose='verify_email' ORDER BY created_at DESC,rowid DESC LIMIT 1", (user_id,),
        ).fetchone()
    status = "verified" if user["email_verified"] else "not_requested"
    if latest and not user["email_verified"]:
        status = latest["delivery_status"]
        if status == "cancelled" or latest["consumed_at"] is not None:
            status = "failed"
        if status == "pending" and latest["created_at"] + MAIL_PENDING_TIMEOUT_SECONDS <= int(time.time()):
            status = "failed"
        if status == "sent" and latest["expires_at"] <= int(time.time()):
            status = "expired"
    return {"status": status, "requested_at": _iso_timestamp(latest["created_at"]) if latest else None,
            "expires_at": _iso_timestamp(latest["expires_at"]) if latest else None}


def prepare_password_reset(email: str, client_ip: str) -> ActionDelivery | None:
    email = normalize_email(email)
    workspace_db.init_db()
    _consume_limits([("reset_request_ip", client_ip, 20, 3600), ("reset_request_email", email, 3, 3600)], "恢复邮件请求过于频繁，请稍后重试")
    # 配置故障在邮箱查找前处理，未知、停用和正常账号响应相同。
    _require_mail()
    with closing(workspace_db.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        user = connection.execute("SELECT * FROM workspace_users WHERE email=? AND is_active=1", (email,)).fetchone()
        return _prepare_action(connection, user, "reset_password", int(time.time())) if user else None


def deliver_password_reset(delivery: ActionDelivery | None) -> None:
    """响应只表示已受理；后台投递失败留脱敏记录，原始 token 不写入 outbox。"""
    if delivery is None:
        return
    try:
        _deliver_action(delivery)
    except IdentityError:
        # SMTP 失败已记入投递状态；此处不向匿名请求泄露账号存在性。
        return
    except (sqlite3.DatabaseError, OSError):
        _LOG.error("Workspace password recovery could not update delivery state; retry required")


def _action_row(connection: sqlite3.Connection, token: str, purpose: str, now: int):
    if not _valid_token(token):
        raise IdentityError(422, "链接无效、已过期或已使用，请重新申请")
    row = connection.execute(
        """SELECT t.*, u.email, u.password_hash FROM workspace_action_tokens t
           JOIN workspace_users u ON u.id=t.user_id
           WHERE t.token_hash=? AND t.purpose=? AND t.expires_at>? AND t.consumed_at IS NULL
           AND t.delivery_status='sent' AND u.is_active=1 AND u.credential_version=t.credential_version""",
        (_token_hash(token), purpose, now),
    ).fetchone()
    if row is None:
        raise IdentityError(422, "链接无效、已过期或已使用，请重新申请")
    return row


def _consume_confirmation_limit(client_ip: str, purpose: str) -> None:
    workspace_db.init_db()
    _consume_limits([(f"{purpose}_confirm_ip", client_ip, 30, 900)], "链接验证过于频繁，请稍后重试")


def confirm_email_verification(token: str, client_ip: str) -> None:
    _consume_confirmation_limit(client_ip, "verify_email")
    with closing(workspace_db.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        action = _action_row(connection, token, "verify_email", now)
        connection.execute("UPDATE workspace_users SET email_verified=1, updated_at=? WHERE id=?", (now, action["user_id"]))
        connection.execute("UPDATE workspace_action_tokens SET consumed_at=?, updated_at=? WHERE user_id=? AND purpose='verify_email' AND consumed_at IS NULL", (now, now, action["user_id"]))


def _replace_password(connection: sqlite3.Connection, user_id: str, encoded: str, now: int) -> None:
    connection.execute("UPDATE workspace_users SET password_hash=?, credential_version=credential_version+1, updated_at=? WHERE id=?", (encoded, now, user_id))
    connection.execute("DELETE FROM workspace_sessions WHERE user_id=?", (user_id,))
    connection.execute("UPDATE workspace_action_tokens SET consumed_at=?, updated_at=? WHERE user_id=? AND consumed_at IS NULL", (now, now, user_id))


def confirm_password_reset(token: str, new_password: str, client_ip: str) -> None:
    _consume_confirmation_limit(client_ip, "reset_password")
    with closing(workspace_db.connect()) as connection:
        initial = _action_row(connection, token, "reset_password", int(time.time()))
    validate_password(new_password, initial["email"])
    encoded = hash_password(new_password)
    with closing(workspace_db.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        action = _action_row(connection, token, "reset_password", now)
        _replace_password(connection, action["user_id"], encoded, now)


def change_password(user_id: str, current_token: str, current_password: str, new_password: str, client_ip: str) -> None:
    workspace_db.init_db()
    _consume_limits([("change_password_user", user_id, 10, 900), ("change_password_ip", client_ip, 30, 900)], "密码校验过于频繁，请稍后重试")
    if not isinstance(current_password, str) or not 1 <= len(current_password) <= 128 or any(0xD800 <= ord(character) <= 0xDFFF for character in current_password):
        raise IdentityError(422, "当前密码格式不正确")
    with closing(workspace_db.connect()) as connection:
        user = _active_session(connection, user_id, current_token)
    if not verify_password(current_password, user["password_hash"]):
        raise IdentityError(403, "当前密码不正确")
    validate_password(new_password, user["email"])
    if current_password == new_password:
        raise IdentityError(422, "新密码不能与当前密码相同")
    encoded = hash_password(new_password)
    with closing(workspace_db.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        current = _active_session(connection, user_id, current_token)
        if current["credential_version"] != user["credential_version"] or current["password_hash"] != user["password_hash"]:
            raise IdentityError(401, "账号凭据已变化，请重新登录")
        _replace_password(connection, user_id, encoded, int(time.time()))
