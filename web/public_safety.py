import os
import re
from typing import Any, Dict


def is_production() -> bool:
    """判断当前 Web 服务是否处于公网生产模式。"""
    return (
        os.environ.get("ENV", "").lower() == "production"
        or os.environ.get("ALPHAPILOT_ENV", "").lower() == "production"
        or os.environ.get("PRODUCTION", "").lower() in ("true", "1")
    )


def is_control_api_enabled() -> bool:
    """控制 API 必须显式开启，公网默认只读。"""
    return os.environ.get("ALPHAPILOT_ENABLE_CONTROL_API", "").lower() in ("true", "1", "yes")


def is_internal_status_exposed() -> bool:
    """生产环境默认不公开内部状态细节。"""
    return os.environ.get("ALPHAPILOT_EXPOSE_INTERNAL_STATUS", "").lower() in ("true", "1", "yes")


def public_error_message() -> str:
    return "公开数据暂时不可用，请稍后刷新"


_SECRET_PATTERNS = [
    re.compile(r"(?i)(token|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*[\w.\-+/=]+"),
    re.compile(r"(?i)authorization\s*[:=]\s*bearer\s+[\w.\-+/=]+"),
    re.compile(r"(?i)(longport|telegram|xiaomi|mimo)[\w_-]*\s*[:=]\s*[\w.\-+/=]+"),
]

_INTERNAL_PATTERNS = [
    re.compile(r"[A-Za-z]:\\[^\s\"'<>]+"),
    re.compile(r"/home/[^\s\"'<>]+"),
    re.compile(r"/root/[^\s\"'<>]+"),
    re.compile(r"python3?\s+main\.py\s+--[\w-]+"),
    re.compile(r"systemctl\s+--user\s+\w+\s+[\w.\-]+"),
    re.compile(r"Traceback \(most recent call last\):[\s\S]*", re.IGNORECASE),
]

_PROMPT_LEAK_MARKERS = (
    "推理推断",
    "用户要求",
    "必须严格返回JSON",
    "严格返回JSON",
    "决策必须是BUY",
    "作为资深A股量化AI交易员",
)


def sanitize_public_text(value: Any, max_len: int = 220) -> str:
    """把公开页面文案压缩为可展示摘要，避免泄露命令、路径、密钥和提示词细节。"""
    if value is None:
        return ""

    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return ""

    if any(marker in text for marker in _PROMPT_LEAK_MARKERS):
        return "模型已输出结构化交易判断，原始长推理已在公开页面脱敏折叠。"

    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[已脱敏]", text)
    for pattern in _INTERNAL_PATTERNS:
        text = pattern.sub("[内部运维信息已隐藏]", text)

    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "..."
    return text


def sanitize_status_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """生成公开仪表盘使用的只读状态快照。"""
    health = snapshot.get("health") or {}
    watchdog = snapshot.get("watchdog") or {}
    control = snapshot.get("control") or {}

    risk_warnings = []
    if not health.get("ok", True):
        risk_warnings.append("环境自检存在异常")
    if not watchdog.get("ok", True):
        risk_warnings.append("自动驾驶守护检测到异常")
    if snapshot.get("crash_open"):
        risk_warnings.append("系统存在未关闭故障")
    if control.get("paused"):
        risk_warnings.append("自动交易当前处于暂停状态")

    recent_logs = []
    for log in (snapshot.get("recent_logs") or [])[:8]:
        recent_logs.append({
            "time": sanitize_public_text(log.get("time"), 16),
            "type": sanitize_public_text(log.get("type"), 32),
            "status": sanitize_public_text(log.get("status"), 24),
            "action": sanitize_public_text(log.get("action"), 120),
            "error": "公开页面已隐藏错误细节" if log.get("error") else "",
        })

    account = snapshot.get("account") or {}
    return {
        "timestamp": snapshot.get("timestamp"),
        "public_mode": True,
        "health": {
            "ok": bool(health.get("ok", True)),
            "failed_count": len(health.get("failed_required") or []),
        },
        "watchdog": {
            "ok": bool(watchdog.get("ok", True)),
            "critical_count": len(watchdog.get("criticals") or []),
        },
        "control": {
            "paused": bool(control.get("paused")),
        },
        "account": {
            "initial_capital": account.get("initial_capital", 0.0),
            "total_assets": account.get("total_assets", 0.0),
            "cash": account.get("cash", 0.0),
            "positions": account.get("positions", []),
            "total_pnl": account.get("total_pnl", 0.0),
            "total_pnl_pct": account.get("total_pnl_pct", 0.0),
        },
        "adaptive": snapshot.get("adaptive") or {},
        "crash_open": bool(snapshot.get("crash_open")),
        "pipeline_progress": snapshot.get("pipeline_progress") or {},
        "recent_logs": recent_logs,
        "risk_warnings": risk_warnings,
        "loop_count": snapshot.get("loop_count", 0),
        "last_loop_time": snapshot.get("last_loop_time", "-"),
    }
