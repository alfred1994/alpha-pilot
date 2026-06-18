"""
自动盯盘Doctor
====================================================================
Watchdog负责发现自动盘停滞/漏动作；Doctor负责在安全边界内尝试自愈：
  1. 先执行Watchdog
  2. 若出现critical且交易通道仍为paper，则触发一轮自动盯盘循环
  3. 再次执行Watchdog，输出修复前后状态

Doctor只做模拟盘自愈，不会绕过 BrokerAdapter 或触发真实交易。
====================================================================
"""
from datetime import datetime
from scheduler.market_calendar import _now_bj
from typing import Callable, Dict, List, Optional, Any

from data.database import Database
from scheduler.auto_trader import run_auto_cycle
from scheduler.control import pause_auto_trader
from scheduler.market_calendar import get_market_status, is_trading_day
from scheduler.watchdog import (
    WatchdogItem,
    format_watchdog_report,
    run_auto_watchdog,
)


NON_RECOVERABLE_CRITICALS = {"交易通道", "自动事件读取"}


def _critical_items(items: List[WatchdogItem]) -> List[WatchdogItem]:
    """提取critical检查项"""
    return [item for item in items if item.severity == "critical"]


def _critical_names(items: List[WatchdogItem]) -> List[str]:
    """提取critical名称"""
    return [item.name for item in _critical_items(items)]


def _can_recover(items: List[WatchdogItem]) -> bool:
    """判断Doctor是否可以尝试自愈"""
    critical_names = set(_critical_names(items))
    return bool(critical_names) and not (critical_names & NON_RECOVERABLE_CRITICALS)


def _insert_doctor_event(date: str, status: str, actions: list,
                         before: List[str], after: List[str],
                         closure_repair: Dict = None,
                         db_path: str = None):
    """记录Doctor事件，失败不影响主流程"""
    try:
        closure_before = ((closure_repair or {}).get("before") or {}).get("overall") or {}
        closure_after = ((closure_repair or {}).get("after") or {}).get("overall") or {}
        closure_critical = closure_after.get("critical") or []
        with Database(db_path=db_path) as db:
            db.insert_auto_event({
                "date": date,
                "event_type": "auto_doctor",
                "status": status,
                "actions": actions,
                "details": {
                    "before_critical": before,
                    "after_critical": after,
                    "closure_gap": ((closure_repair or {}).get("gap") or {}).get("name", ""),
                    "closure_before_level": closure_before.get("level", ""),
                    "closure_after_level": closure_after.get("level", ""),
                    "closure_after_critical": closure_critical,
                },
                "error": "; ".join(after + closure_critical),
            })
    except Exception:
        pass


def run_auto_doctor(
    *,
    recover: bool = True,
    force_scan: bool = False,
    now: datetime = None,
    status_override: str = None,
    trading_day_override: bool = None,
    state_file: str = None,
    lock_file: str = None,
    db_path: str = None,
    control_file: str = None,
    services: Optional[Dict[str, Callable[..., Any]]] = None,
    notify: bool = False,
    auto_pause_on_failure: bool = True,
    repair_closure: bool = True,
    closure_repair_func: Callable[..., Dict] = None,
    max_loop_lag_sec: int = None,
    max_scan_lag_sec: int = None,
    max_stop_lag_sec: int = None,
) -> Dict:
    """
    执行自动盯盘巡检与自愈

    Args:
        recover: 是否允许触发一轮自愈循环
        force_scan: 自愈循环是否强制盘中扫描
        now/status_override/trading_day_override: 测试和演练注入项
        state_file/lock_file/db_path: 状态文件、锁文件和SQLite路径，None时使用默认正式模拟盘路径
        control_file: 控制状态文件路径，None时使用默认data/auto_control.json
        services: 自动循环外部服务替身，便于测试隔离
        notify: 自愈循环是否发送通知
        auto_pause_on_failure: 自愈失败后是否自动暂停盘中交易动作
        repair_closure: Watchdog自愈后是否继续尝试闭环缺口自愈
        closure_repair_func: 闭环自愈函数注入，便于测试

    Returns:
        包含before/after/recovery/actions的结果字典
    """
    now = now or _now_bj()
    today = now.strftime("%Y-%m-%d")
    status = status_override or get_market_status()
    trading_day = trading_day_override if trading_day_override is not None else is_trading_day(today)

    watchdog_kwargs = {
        "now": now,
        "status_override": status,
        "trading_day_override": trading_day,
        "state_file": state_file,
        "lock_file": lock_file,
        "control_file": control_file,
        "db_path": db_path,
        "max_loop_lag_sec": max_loop_lag_sec,
        "max_scan_lag_sec": max_scan_lag_sec,
        "max_stop_lag_sec": max_stop_lag_sec,
    }
    before_items = run_auto_watchdog(**watchdog_kwargs)
    before_critical = _critical_names(before_items)
    actions = []
    recovery_result = None

    if not before_critical:
        actions.append("Watchdog正常，无需自愈")
    elif not recover:
        actions.append("发现critical，但recover=False，未执行自愈")
    elif not _can_recover(before_items):
        actions.append("发现不可自愈critical，保持人工介入")
    else:
        actions.append(f"发现critical: {', '.join(before_critical)}")
        recovery_result = run_auto_cycle(
            force_scan=force_scan or status == "盘中",
            status_override=status,
            trading_day_override=trading_day,
            today_override=today,
            now_override=now,
            services=services,
            persist_state=True,
            record_event=True,
            db_path=db_path,
            state_file=state_file,
            control_file=control_file,
            notify=notify,
        )
        actions.append(
            f"已触发自愈循环: {status} 动作{len(recovery_result.get('actions', []))}项"
        )

    after_items = run_auto_watchdog(**watchdog_kwargs)
    after_critical = _critical_names(after_items)
    pause_state = None
    if before_critical and not after_critical:
        actions.append("自愈后critical已清除")
    elif before_critical and after_critical:
        actions.append(f"自愈后仍有critical: {', '.join(after_critical)}")
        if auto_pause_on_failure:
            pause_state = pause_auto_trader(
                reason=f"doctor unresolved critical: {', '.join(after_critical)}",
                control_file=control_file,
                updated_by="auto_doctor",
            )
            actions.append("自愈失败，已自动暂停盘中交易动作")
            # 暂停后再复查一次，让报告展示新的控制状态。
            after_items = run_auto_watchdog(**watchdog_kwargs)
            after_critical = _critical_names(after_items)
        else:
            pause_state = None

    closure_repair = None
    if repair_closure and not after_critical:
        try:
            if closure_repair_func is None:
                from scheduler.closure_repair import run_closure_repair
                closure_repair_func = run_closure_repair
            closure_repair = closure_repair_func(
                date=today,
                now=now,
                db_path=db_path,
                state_file=state_file,
                lock_file=lock_file,
                control_file=control_file,
                recover=recover,
            )
            gap_name = (closure_repair.get("gap") or {}).get("name", "无")
            after_level = ((closure_repair.get("after") or {}).get("overall") or {}).get("level")
            actions.append(f"闭环自愈检查: gap={gap_name} after={after_level}")
        except Exception as e:
            actions.append(f"闭环自愈检查失败: {e}")

    _insert_doctor_event(
        date=today,
        status=status,
        actions=actions,
        before=before_critical,
        after=after_critical,
        closure_repair=closure_repair,
        db_path=db_path,
    )
    closure_after_critical = (
        (((closure_repair or {}).get("after") or {}).get("overall") or {}).get("critical")
        or []
    )

    return {
        "date": today,
        "status": status,
        "before_items": before_items,
        "after_items": after_items,
        "before_critical": before_critical,
        "after_critical": after_critical,
        "recovery_result": recovery_result,
        "actions": actions,
        "pause_state": pause_state,
        "closure_repair": closure_repair,
        "closure_after_critical": closure_after_critical,
        "recovered": bool(before_critical) and not after_critical,
        "success": not after_critical and not closure_after_critical,
    }


def format_doctor_report(result: Dict) -> str:
    """格式化Doctor报告"""
    lines = [
        "=" * 60,
        "自动盯盘Doctor",
        "=" * 60,
        f"日期: {result.get('date')} 状态: {result.get('status')}",
        f"修复前critical: {len(result.get('before_critical', []))}",
        f"修复后critical: {len(result.get('after_critical', []))}",
        "",
        "动作:",
    ]
    for action in result.get("actions", []):
        lines.append(f"  - {action}")

    lines.extend([
        "",
        "修复后Watchdog:",
        format_watchdog_report(result.get("after_items", [])),
    ])
    if result.get("closure_repair"):
        closure = result["closure_repair"]
        lines.extend([
            "",
            "闭环自愈:",
            f"  缺口: {(closure.get('gap') or {}).get('name', '无')}",
            f"  自愈后等级: {((closure.get('after') or {}).get('overall') or {}).get('level', '')}",
        ])
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_doctor_report(run_auto_doctor()))
