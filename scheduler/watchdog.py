"""
自动盯盘Watchdog
====================================================================
用于无人值守运行时检查自动交易员是否还在正常工作：
  - 自动循环状态是否新鲜
  - 今日是否出现异常事件
  - 盘前是否完成预热和市场环境识别
  - 盘中是否按间隔扫描、执行和止损巡检
  - 盘后是否完成复盘进化

该模块只读取状态和SQLite事件，不触发行情、LLM或交易。
====================================================================
"""
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from scheduler.market_calendar import _now_bj
from typing import List, Optional

from config import (
    AUTO_LOOP_INTERVAL,
    AUTO_REVIEW_AFTER,
    AUTO_SCAN_INTERVAL,
    AUTO_STOP_INTERVAL,
    BROKER_MODE,
)
from data.database import Database
from scheduler.auto_trader import AUTO_LOCK_FILE, AUTO_STATE_FILE
from scheduler.control import get_auto_control_state
from scheduler.market_calendar import get_market_status, is_trading_day


@dataclass
class WatchdogItem:
    """Watchdog检查项"""
    name: str
    ok: bool
    severity: str = "ok"  # ok/warn/critical
    detail: str = ""
    suggestion: str = ""


def _load_state(state_file: str) -> dict:
    """读取自动盯盘状态文件"""
    if not os.path.exists(state_file):
        return {}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_lock(lock_file: str) -> dict:
    """读取自动盯盘单实例锁"""
    if not os.path.exists(lock_file):
        return {}
    try:
        with open(lock_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _parse_time_hhmm(value: str) -> tuple:
    """解析HH:MM配置"""
    try:
        hh, mm = value.split(":", 1)
        return int(hh), int(mm)
    except Exception:
        return 15, 5


def _parse_iso(value: str) -> Optional[datetime]:
    """解析ISO时间字符串"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _stale_seconds(updated_at: str, now: datetime) -> Optional[float]:
    """计算状态更新时间距今秒数"""
    updated = _parse_iso(updated_at)
    if not updated:
        return None
    return max(0.0, (now - updated).total_seconds())


def _after_review_time(now: datetime) -> bool:
    """判断是否到达盘后复盘窗口"""
    hh, mm = _parse_time_hhmm(AUTO_REVIEW_AFTER)
    return (now.hour, now.minute) >= (hh, mm)


def _status_is_active(status: str) -> bool:
    """判断当前状态是否需要自动循环保持活跃"""
    return status in ("盘前", "集合竞价", "盘中", "午休", "盘后")


def _event_has_error(event: dict) -> bool:
    """判断自动事件是否包含异常"""
    if event.get("event_type") in ("auto_doctor", "ops_status"):
        return False
    if event.get("error"):
        return True
    return any(str(action).startswith("异常") for action in event.get("actions", []))


def _make_item(name: str, ok: bool, detail: str,
               severity: str = None, suggestion: str = "") -> WatchdogItem:
    """构造检查项"""
    if severity is None:
        severity = "ok" if ok else "warn"
    return WatchdogItem(name=name, ok=ok, severity=severity, detail=detail, suggestion=suggestion)


def run_auto_watchdog(
    *,
    now: datetime = None,
    status_override: str = None,
    trading_day_override: bool = None,
    state_file: str = None,
    lock_file: str = None,
    control_file: str = None,
    db_path: str = None,
    max_loop_lag_sec: int = None,
    max_scan_lag_sec: int = None,
    max_stop_lag_sec: int = None,
) -> List[WatchdogItem]:
    """
    执行自动盯盘运行态检查

    Args:
        now: 检查时间，默认当前时间
        status_override: 覆盖市场状态，便于测试
        trading_day_override: 覆盖交易日判断，便于测试
        state_file: 自动盯盘状态文件路径
        lock_file: 自动盯盘长驻进程锁路径
        control_file: 自动盯盘控制状态文件路径
        db_path: SQLite路径
        max_loop_lag_sec: 自动循环最大滞后秒数
        max_scan_lag_sec: 盘中扫描最大滞后秒数
        max_stop_lag_sec: 止损巡检最大滞后秒数

    Returns:
        WatchdogItem列表
    """
    now = now or _now_bj()
    today = now.strftime("%Y-%m-%d")
    status = status_override or get_market_status()
    is_today_trading = trading_day_override if trading_day_override is not None else is_trading_day(today)
    state_file = state_file or AUTO_STATE_FILE
    lock_file = lock_file or AUTO_LOCK_FILE
    state = _load_state(state_file)
    lock_state = _load_lock(lock_file)
    control_state = get_auto_control_state(control_file=control_file)
    paused = bool(control_state.get("paused"))
    max_loop_lag_sec = max_loop_lag_sec or max(180, AUTO_LOOP_INTERVAL * 3)
    max_scan_lag_sec = max_scan_lag_sec or max(AUTO_SCAN_INTERVAL + 300, int(AUTO_SCAN_INTERVAL * 1.5))
    max_stop_lag_sec = max_stop_lag_sec or max(180, AUTO_STOP_INTERVAL * 3)

    items: List[WatchdogItem] = []

    items.append(_make_item(
        "交易通道",
        BROKER_MODE == "paper",
        f"mode={BROKER_MODE}",
        "critical" if BROKER_MODE != "paper" else "ok",
        "当前目标是模拟盘，请保持 BROKER_MODE=paper。",
    ))

    lock_lag = _stale_seconds(lock_state.get("updated_at", ""), now) if lock_state else None
    if not lock_state:
        active = is_today_trading and _status_is_active(status)
        items.append(_make_item(
            "自动盘锁",
            not active,
            f"未发现长驻锁: {lock_file}",
            "warn" if active else "ok",
            "如果需要无人值守，请启动 python main.py --auto 或安装计划任务。",
        ))
    elif lock_lag is None:
        items.append(_make_item(
            "自动盘锁",
            False,
            f"锁文件不可解析或缺少updated_at: {lock_file}",
            "critical" if is_today_trading and _status_is_active(status) else "warn",
            "删除过期锁后重启 python main.py --auto。",
        ))
    else:
        ok = (not is_today_trading) or (not _status_is_active(status)) or lock_lag <= max_loop_lag_sec
        items.append(_make_item(
            "自动盘锁",
            ok,
            f"pid={lock_state.get('pid')} 锁心跳{lock_lag:.0f}秒前，阈值{max_loop_lag_sec}秒",
            "critical" if not ok else "ok",
            "自动盘长驻进程锁已过期，检查进程并重启 --auto。",
        ))

    if not state:
        severity = "critical" if is_today_trading and _status_is_active(status) else "warn"
        items.append(_make_item(
            "自动盯盘状态",
            False,
            f"状态文件不存在或不可读: {state_file}",
            severity,
            "先运行 python main.py --auto-once 或启动 --auto。",
        ))
    else:
        lag = _stale_seconds(state.get("updated_at", ""), now)
        if lag is None:
            items.append(_make_item(
                "自动循环新鲜度",
                False,
                "updated_at为空或格式错误",
                "critical" if is_today_trading and _status_is_active(status) else "warn",
                "检查自动循环是否仍在运行。",
            ))
        else:
            ok = (not is_today_trading) or (not _status_is_active(status)) or lag <= max_loop_lag_sec
            items.append(_make_item(
                "自动循环新鲜度",
                ok,
                f"最近更新{lag:.0f}秒前，阈值{max_loop_lag_sec}秒",
                "critical" if not ok else "ok",
                "自动循环可能已停滞，重启 python main.py --auto。",
            ))

        if state.get("last_error"):
            items.append(_make_item(
                "最近循环错误",
                False,
                state.get("last_error", ""),
                "critical",
                "查看日志和auto_events.error定位异常。",
            ))
        else:
            items.append(_make_item("最近循环错误", True, "无", "ok"))

    try:
        with Database(db_path=db_path) as db:
            events = db.get_auto_events(date=today, limit=100)
    except Exception as e:
        events = []
        items.append(_make_item("自动事件读取", False, str(e), "critical", "检查SQLite数据库。"))

    error_events = [e for e in events if _event_has_error(e)]
    items.append(_make_item(
        "今日异常事件",
        len(error_events) == 0,
        f"{len(error_events)}条异常 / {len(events)}条事件",
        "critical" if error_events else "ok",
        "查看 data/quant.db 的 auto_events 表。",
    ))
    items.append(_make_item(
        "自动交易控制",
        True,
        f"{'暂停' if paused else '运行'} reason={control_state.get('reason', '')}",
        "ok",
    ))

    if not is_today_trading:
        items.append(_make_item("市场阶段履约", True, f"{today}休市，无需交易动作", "ok"))
        return items

    if state:
        if status in ("盘前", "集合竞价") and (now.hour, now.minute) >= (8, 45):
            prefetch_ok = state.get("last_prefetch_date") == today
            regime_ok = state.get("last_regime_date") == today
            items.append(_make_item(
                "盘前预热",
                prefetch_ok,
                f"last_prefetch_date={state.get('last_prefetch_date')}",
                "warn" if not prefetch_ok else "ok",
                "盘前应完成数据预热。",
            ))
            items.append(_make_item(
                "盘前市场识别",
                regime_ok,
                f"last_regime_date={state.get('last_regime_date')}",
                "warn" if not regime_ok else "ok",
                "盘前应完成市场环境识别。",
            ))

        elif status == "盘中" and paused:
            items.append(_make_item(
                "盘中交易动作",
                True,
                "已暂停，跳过扫描/执行/止损巡检",
                "ok",
            ))

        elif status == "盘中":
            now_ts = time.mktime(now.timetuple())
            last_scan_at = float(state.get("last_scan_at") or 0)
            last_execute_at = float(state.get("last_execute_at") or 0)
            last_stop_at = float(state.get("last_stop_check_at") or 0)
            scan_lag = now_ts - last_scan_at if last_scan_at else None
            execute_lag = now_ts - last_execute_at if last_execute_at else None
            stop_lag = now_ts - last_stop_at if last_stop_at else None

            scan_ok = scan_lag is not None and scan_lag <= max_scan_lag_sec
            execute_ok = execute_lag is not None and execute_lag <= max_scan_lag_sec
            stop_ok = stop_lag is not None and stop_lag <= max_stop_lag_sec
            items.append(_make_item(
                "盘中扫描",
                scan_ok,
                f"距上次扫描{scan_lag:.0f}秒，阈值{max_scan_lag_sec}秒" if scan_lag is not None else "尚无扫描记录",
                "critical" if not scan_ok else "ok",
                "盘中扫描停滞，检查 --auto 是否运行。",
            ))
            items.append(_make_item(
                "盘中模拟执行",
                execute_ok,
                f"距上次执行{execute_lag:.0f}秒，阈值{max_scan_lag_sec}秒" if execute_lag is not None else "尚无执行记录",
                "critical" if not execute_ok else "ok",
                "扫描后应执行模拟交易计划。",
            ))
            items.append(_make_item(
                "盘中止损巡检",
                stop_ok,
                f"距上次巡检{stop_lag:.0f}秒，阈值{max_stop_lag_sec}秒" if stop_lag is not None else "尚无止损巡检记录",
                "critical" if not stop_ok else "ok",
                "止损巡检停滞，检查行情源和自动循环。",
            ))

        elif status == "盘后" and _after_review_time(now):
            review_ok = state.get("last_review_date") == today
            items.append(_make_item(
                "盘后复盘进化",
                review_ok,
                f"last_review_date={state.get('last_review_date')}",
                "critical" if not review_ok else "ok",
                "盘后复盘未完成，运行 python main.py --review 或等待 --auto 执行。",
            ))
        else:
            items.append(_make_item("市场阶段履约", True, f"{status}保持监控", "ok"))

    return items


def format_watchdog_report(items: List[WatchdogItem]) -> str:
    """格式化Watchdog报告"""
    lines = [
        "=" * 60,
        "自动盯盘Watchdog",
        "=" * 60,
    ]
    critical = 0
    warn = 0
    for item in items:
        label = item.severity.upper() if item.severity != "ok" else "OK"
        if item.severity == "critical":
            critical += 1
        elif item.severity == "warn":
            warn += 1
        lines.append(f"[{label}] {item.name} - {item.detail}")
        if not item.ok and item.suggestion:
            lines.append(f"  建议: {item.suggestion}")

    lines.append("-" * 60)
    if critical:
        lines.append(f"结论: 自动盯盘需要人工介入，critical={critical} warn={warn}")
    elif warn:
        lines.append(f"结论: 自动盯盘可继续运行，但有 warn={warn}")
    else:
        lines.append("结论: 自动盯盘运行态正常")
    lines.append("=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_watchdog_report(run_auto_watchdog()))
