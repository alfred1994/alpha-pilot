"""显式启用的工作区邮件投递，只通过经过验证的 TLS SMTP 连接发送。"""

from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr
import os
import re
import smtplib
import ssl
from urllib.parse import urlsplit


SMTP_TIMEOUT_SECONDS = 8


class MailError(Exception):
    """公开错误不保留 SMTP 响应、凭据、收件人或邮件正文。"""


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    sender: str
    origin: str
    tls_mode: str


def _configured_origin(value: str) -> str:
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("invalid origin")
    parts = urlsplit(value)
    if (
        parts.scheme != "https" or not parts.hostname or parts.username or parts.password
        or parts.path not in {"", "/"} or parts.query or parts.fragment
    ):
        raise ValueError("invalid origin")
    hostname = parts.hostname.encode("idna").decode("ascii").lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port = parts.port
    return f"https://{hostname}" + (f":{port}" if port not in {None, 443} else "")


def configuration() -> MailConfig:
    if os.environ.get("ALPHAPILOT_MAIL_ENABLED", "false").strip().lower() not in {"1", "true", "yes"}:
        raise MailError("邮件服务尚未启用")
    host = os.environ.get("SMTP_HOST", "").strip()
    username = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip()
    mode = os.environ.get("SMTP_TLS_MODE", "starttls").strip().lower()
    try:
        port = int(os.environ.get("SMTP_PORT", ""))
        origin = _configured_origin(os.environ.get("ALPHAPILOT_WORKSPACE_ORIGIN", "").strip())
        if (
            not host or len(host) > 253 or any(character.isspace() for character in host)
            or any(character in host for character in "/\\@?#")
            or not 1 <= port <= 65535 or not username or not password
            or mode not in {"starttls", "ssl"}
            or not sender or len(sender) > 254 or "@" not in sender
            or parseaddr(sender) != ("", sender)
            or any(ord(character) < 32 or ord(character) == 127 for character in sender + host + username)
        ):
            raise ValueError("invalid SMTP configuration")
    except (ValueError, UnicodeError):
        raise MailError("邮件服务配置不可用") from None
    return MailConfig(host, port, username, password, sender, origin, mode)


def available() -> bool:
    """仅检查显式配置；不发测试邮件，也不声称 SMTP 已实际连通。"""
    try:
        configuration()
        return True
    except MailError:
        return False


def send_action_email(recipient: str, purpose: str, token: str) -> None:
    settings = configuration()
    if (
        purpose not in {"verify_email", "reset_password"}
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
        or not recipient or parseaddr(recipient) != ("", recipient)
        or any(ord(character) < 32 for character in recipient)
    ):
        raise MailError("邮件内容不可用")
    verification = purpose == "verify_email"
    action = "verify-email" if verification else "reset-password"
    link = f"{settings.origin}/workspace/#{action}={token}"
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = recipient
    message["Subject"] = "验证你的 AlphaPilot 邮箱" if verification else "重置你的 AlphaPilot 密码"
    duration = "24 小时" if verification else "30 分钟"
    message.set_content(
        f"请在 {duration}内打开下面的链接，并在页面中确认操作：\n\n{link}\n\n"
        "链接只能使用一次；重新申请链接或修改密码会使旧链接失效。\n"
        "如果不是你本人发起的请求，请忽略这封邮件。请勿转发此链接。\n"
    )
    context = ssl.create_default_context()
    try:
        if settings.tls_mode == "ssl":
            smtp = smtplib.SMTP_SSL(settings.host, settings.port, timeout=SMTP_TIMEOUT_SECONDS, context=context)
        else:
            smtp = smtplib.SMTP(settings.host, settings.port, timeout=SMTP_TIMEOUT_SECONDS)
        with smtp as smtp:
            smtp.ehlo()
            if settings.tls_mode == "starttls":
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(settings.username, settings.password)
            refused = smtp.send_message(message, from_addr=settings.sender, to_addrs=[recipient])
            if refused:
                raise MailError("邮件暂时无法投递，请稍后重试")
    except (smtplib.SMTPException, OSError, ValueError):
        raise MailError("邮件暂时无法投递，请稍后重试") from None
