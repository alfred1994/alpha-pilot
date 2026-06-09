"""
系统健康检查
====================================================================
用于启动自动盯盘前检查运行环境、依赖、数据库和关键配置。
====================================================================
"""
import importlib
import os
from dataclasses import dataclass
from typing import List


@dataclass
class HealthItem:
    """健康检查项"""
    name: str
    ok: bool
    detail: str = ""
    required: bool = True


def _check_module(name: str, required: bool = True) -> HealthItem:
    try:
        importlib.import_module(name)
        return HealthItem(f"依赖:{name}", True, "已安装", required)
    except Exception as e:
        return HealthItem(f"依赖:{name}", False, str(e), required)


def _check_env(name: str, required: bool = False) -> HealthItem:
    value = os.environ.get(name, "")
    if value:
        return HealthItem(f"环境变量:{name}", True, "已配置", required)
    return HealthItem(f"环境变量:{name}", not required, "未配置，将使用降级能力" if not required else "未配置", required)


def run_health_check(db_path: str = None, control_file: str = None) -> List[HealthItem]:
    """执行健康检查"""
    items = []

    # 基础依赖
    for mod in ["pandas", "numpy", "requests"]:
        items.append(_check_module(mod, required=True))

    # 外部数据源依赖（可降级，但建议安装）
    for mod in ["akshare", "baostock", "longport"]:
        items.append(_check_module(mod, required=False))

    # 数据库初始化
    stats = {}
    try:
        from data.database import Database
        with Database(db_path=db_path) as db:
            stats = db.get_stats()
        items.append(HealthItem("SQLite数据库", True, f"可用，表统计={stats}", True))
    except Exception as e:
        items.append(HealthItem("SQLite数据库", False, str(e), True))

    # 交易通道
    try:
        from execution.broker import get_broker_adapter
        broker = get_broker_adapter()
        items.append(HealthItem("交易通道", True, f"mode={broker.mode}", True))
    except Exception as e:
        items.append(HealthItem("交易通道", False, str(e), True))

    # 交易记忆闭环：复盘教训/决策记录/prompt进化建议
    try:
        from data.database import Database
        with Database(db_path=db_path) as db:
            lessons = stats.get("lessons", 0)
            decisions = stats.get("llm_decisions", 0)
            try:
                row = db.conn.execute(
                    "SELECT COUNT(*) AS cnt FROM active_prompt_hints WHERE active = 1"
                ).fetchone()
                hints = row["cnt"] if row else 0
            except Exception:
                hints = 0
        items.append(HealthItem(
            "交易记忆闭环",
            True,
            f"教训{lessons}条 LLM决策{decisions}条 active_hints={hints}",
            False,
        ))
    except Exception as e:
        items.append(HealthItem("交易记忆闭环", False, str(e), False))

    # 自动盯盘通知
    try:
        from config import AUTO_NOTIFY_ENABLED
        from scheduler.notifier import is_configured
        if AUTO_NOTIFY_ENABLED and is_configured():
            detail = "已启用，Telegram已配置"
        elif AUTO_NOTIFY_ENABLED:
            detail = "已启用，Telegram未配置，将静默跳过"
        else:
            detail = "已关闭"
        items.append(HealthItem("自动盯盘通知", True, detail, False))
    except Exception as e:
        items.append(HealthItem("自动盯盘通知", False, str(e), False))

    # 自动盯盘控制开关
    try:
        from scheduler.control import get_auto_control_state
        control = get_auto_control_state(control_file=control_file)
        paused = bool(control.get("paused"))
        detail = (
            f"暂停中 reason={control.get('reason', '')}"
            if paused else
            f"运行中 reason={control.get('reason', '')}"
        )
        items.append(HealthItem("自动盯盘控制", not paused, detail, False))
    except Exception as e:
        items.append(HealthItem("自动盯盘控制", False, str(e), False))

    # API Key：模拟盘可降级
    for env in [
        "XIAOMI_API_KEY",
        "LONGPORT_APP_KEY",
        "LONGPORT_APP_SECRET",
        "LONGPORT_ACCESS_TOKEN",
        "HT_APIKEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]:
        items.append(_check_env(env, required=False))

    return items


def format_health_report(items: List[HealthItem]) -> str:
    """格式化健康检查报告"""
    lines = [
        "=" * 60,
        "系统健康检查",
        "=" * 60,
    ]

    required_failed = 0
    optional_failed = 0
    for item in items:
        status = "OK" if item.ok else ("FAIL" if item.required else "WARN")
        if not item.ok and item.required:
            required_failed += 1
        elif not item.ok:
            optional_failed += 1
        lines.append(f"[{status}] {item.name} - {item.detail}")

    lines.append("-" * 60)
    if required_failed:
        lines.append(f"结论: 不可自动运行，必需项失败 {required_failed} 个")
    elif optional_failed:
        lines.append(f"结论: 可运行，但有 {optional_failed} 个可选能力会降级")
    else:
        lines.append("结论: 自动盯盘基础条件正常")
    lines.append("=" * 60)
    return "\n".join(lines)
