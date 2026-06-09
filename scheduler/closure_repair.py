"""
正式模拟盘闭环缺口自愈
====================================================================
在闭环诊断发现缺口后，只在安全边界内尝试补齐：
  - 盘中缺扫描/执行/决策：触发有限轮 paper-observe 强制扫描
  - 盘后缺复盘/教训：触发一次 run_review
  - 盘前缺准备：触发一次 paper-observe

该模块默认保持 BROKER_MODE=paper，不连接真实交易通道。
====================================================================
"""
import os
from datetime import datetime
from typing import Callable, Dict

from scheduler.closure_check import format_closure_check, run_closure_check
from scheduler.utils import force_paper_mode


RECOVERABLE_GAPS = {
    "盘前准备",
    "盘中扫描",
    "模拟执行",
    "交易决策",
    "盘后复盘",
    "教训沉淀",
}


def _first_recoverable_gap(closure: Dict) -> Dict:
    """选择第一个可自愈缺口"""
    priority = ["盘中扫描", "模拟执行", "交易决策", "盘后复盘", "教训沉淀", "盘前准备"]
    items = {
        item.get("name"): item
        for item in closure.get("items", [])
        if item.get("severity") in ("critical", "warn")
    }
    for name in priority:
        item = items.get(name)
        if item and name in RECOVERABLE_GAPS:
            return item
    return {}


def _insert_repair_event(date: str, status: str, actions: list,
                         before: Dict, after: Dict, db_path: str = None):
    """记录闭环自愈事件，失败不影响主流程"""
    try:
        from data.database import Database
        with Database(db_path=db_path) as db:
            db.insert_auto_event({
                "date": date,
                "event_type": "closure_repair",
                "status": status,
                "actions": actions,
                "details": {
                    "before_level": (before.get("overall") or {}).get("level"),
                    "after_level": (after.get("overall") or {}).get("level"),
                    "before_critical": (before.get("overall") or {}).get("critical", []),
                    "after_critical": (after.get("overall") or {}).get("critical", []),
                    "before_warn": (before.get("overall") or {}).get("warn", []),
                    "after_warn": (after.get("overall") or {}).get("warn", []),
                },
                "error": "; ".join((after.get("overall") or {}).get("critical", [])),
            })
    except Exception:
        pass


def run_closure_repair(
    *,
    date: str = None,
    now: datetime = None,
    db_path: str = None,
    state_file: str = None,
    lock_file: str = None,
    control_file: str = None,
    recover: bool = True,
    closure: Dict = None,
    observe_func: Callable[..., Dict] = None,
    review_func: Callable[[], object] = None,
    closure_func: Callable[..., Dict] = None,
) -> Dict:
    """
    执行一次闭环缺口自愈

    Args中的函数参数用于测试注入。默认不会安装任务，也不会连接实盘。
    """
    force_paper_mode()
    now = now or datetime.now()
    target_date = date or now.strftime("%Y-%m-%d")
    closure_func = closure_func or run_closure_check
    before = closure or closure_func(
        date=target_date,
        now=now,
        db_path=db_path,
        control_file=control_file,
    )
    actions = []
    repair_result = None
    gap = _first_recoverable_gap(before)
    status = before.get("market_status", "")

    if not gap:
        actions.append("未发现可自愈闭环缺口")
    elif not recover:
        actions.append(f"发现闭环缺口 {gap.get('name')}，recover=False，未执行自愈")
    elif gap.get("name") in ("盘中扫描", "模拟执行", "交易决策"):
        if status != "盘中":
            actions.append(f"缺口 {gap.get('name')} 需要盘中窗口，当前{status}，等待下一交易时段")
        else:
            from scheduler.paper_observer import run_paper_observer
            observe_func = observe_func or run_paper_observer
            repair_result = observe_func(
                cycles=1,
                interval=0,
                force_scan=True,
                db_path=db_path,
                state_file=state_file,
                lock_file=lock_file,
                control_file=control_file,
                save_report=True,
                report_days=1,
                report_end_date=target_date,
            )
            actions.append(f"已触发盘中强制观察: {gap.get('name')}")
    elif gap.get("name") in ("盘后复盘", "教训沉淀"):
        if status != "盘后":
            actions.append(f"缺口 {gap.get('name')} 需要盘后窗口，当前{status}，等待盘后")
        else:
            if review_func is None:
                from scheduler.pipeline import run_review
                review_func = run_review
            repair_result = review_func()
            actions.append(f"已触发盘后复盘: {gap.get('name')}")
    elif gap.get("name") == "盘前准备":
        if status not in ("盘前", "集合竞价"):
            actions.append(f"缺口盘前准备需要盘前窗口，当前{status}，等待下一交易日")
        else:
            from scheduler.paper_observer import run_paper_observer
            observe_func = observe_func or run_paper_observer
            repair_result = observe_func(
                cycles=1,
                interval=0,
                force_scan=False,
                db_path=db_path,
                state_file=state_file,
                lock_file=lock_file,
                control_file=control_file,
                save_report=True,
                report_days=1,
                report_end_date=target_date,
            )
            actions.append("已触发盘前观察")

    after = closure_func(
        date=target_date,
        now=now,
        db_path=db_path,
        control_file=control_file,
    )
    before_level = (before.get("overall") or {}).get("level")
    after_level = (after.get("overall") or {}).get("level")
    if before_level != after_level:
        actions.append(f"闭环缺口等级变化: {before_level} -> {after_level}")
    else:
        actions.append(f"闭环缺口等级保持: {after_level}")

    _insert_repair_event(
        date=target_date,
        status=status,
        actions=actions,
        before=before,
        after=after,
        db_path=db_path,
    )

    return {
        "date": target_date,
        "status": status,
        "before": before,
        "after": after,
        "gap": gap,
        "actions": actions,
        "repair_result": repair_result,
        "recovered": (before.get("overall") or {}).get("level") != (after.get("overall") or {}).get("level"),
        "success": not ((after.get("overall") or {}).get("critical")),
    }


def format_closure_repair(result: Dict) -> str:
    """格式化闭环自愈结果"""
    before_level = ((result.get("before") or {}).get("overall") or {}).get("level", "")
    after_level = ((result.get("after") or {}).get("overall") or {}).get("level", "")
    lines = [
        "=" * 60,
        "正式模拟盘闭环自愈",
        "=" * 60,
        f"日期: {result.get('date', '')} 状态: {result.get('status', '')}",
        f"缺口: {(result.get('gap') or {}).get('name', '无')}",
        f"等级: {before_level} -> {after_level}",
        "动作:",
    ]
    for action in result.get("actions", []):
        lines.append(f"  - {action}")
    lines.extend([
        "",
        "自愈后闭环诊断:",
        format_closure_check(result.get("after") or {}),
    ])
    return "\n".join(lines)
