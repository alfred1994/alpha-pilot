"""
正式模拟盘观察运行器
====================================================================
用于在不安装Windows计划任务的情况下，手动启动一段有限轮数的自动盯盘。

该入口写入正式SQLite库和正式状态文件，适合积累真实时钟下的模拟盘证据；
同时强制要求 BROKER_MODE=paper，不连接真实交易通道。
====================================================================
"""
import os
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from config import BASE_DIR
from scheduler.auto_trader import (
    AutoLoopLock,
    AutoTraderState,
    _load_state,
    run_auto_cycle,
)
from scheduler.utils import force_paper_mode


DEFAULT_OBSERVE_LOG = os.path.join(BASE_DIR, "logs", "paper_observe.log")


def _write_log(log_file: str, text: str):
    """追加写入观察日志"""
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")


def _sequence_value(values: Optional[List], index: int):
    """读取测试注入序列中的当前值"""
    if not values:
        return None
    if index < len(values):
        return values[index]
    return values[-1]


def run_paper_observer(
    *,
    cycles: int = 3,
    interval: int = 60,
    force_scan: bool = False,
    log_file: str = None,
    lock_file: str = None,
    state_file: str = None,
    db_path: str = None,
    control_file: str = None,
    services: Optional[Dict[str, Callable]] = None,
    status_sequence: Optional[List[str]] = None,
    now_sequence: Optional[List[datetime]] = None,
    trading_day_override: bool = None,
    sleep_func: Callable[[float], None] = None,
    use_lock: bool = True,
    notify: bool = False,
    save_report: bool = True,
    report_days: int = 5,
    report_end_date: str = None,
    report_output_dir: str = None,
    report_func: Callable[..., Dict] = None,
) -> Dict:
    """
    运行有限轮正式模拟盘观察

    Args:
        cycles: 观察轮数
        interval: 轮间等待秒数
        force_scan: 盘中是否强制扫描
        log_file: 观察日志路径，默认 logs/paper_observe.log
        lock_file/state_file/db_path/control_file: 便于测试隔离的路径
        services/status_sequence/now_sequence/trading_day_override: 测试注入项
        sleep_func: 测试注入的等待函数
        use_lock: 是否获取自动盘单实例锁
        notify: 是否发送通知，默认关闭，避免手动观察刷屏
        save_report: 观察结束后是否保存AI交易员正式报告
        report_days/report_end_date: 报告回看范围
        report_output_dir: 报告输出目录，None时使用默认正式报告目录
        report_func: 测试注入的报告生成函数

    Returns:
        观察摘要
    """
    force_paper_mode()

    cycles = max(1, int(cycles or 1))
    interval = max(0, int(interval or 0))
    log_file = os.path.abspath(log_file or DEFAULT_OBSERVE_LOG)
    sleep_func = sleep_func or time.sleep
    started_at = datetime.now()
    reports = []
    errors = []
    lock = None

    _write_log(
        log_file,
        (
            f"===== {started_at.strftime('%Y-%m-%d %H:%M:%S')} START paper-observe "
            f"cycles={cycles} interval={interval} force_scan={force_scan} ====="
        ),
    )

    try:
        if use_lock:
            lock = AutoLoopLock(lock_file=lock_file).acquire()
            _write_log(log_file, f"lock_acquired={lock.lock_file}")

        state = _load_state(state_file)
        for index in range(cycles):
            if lock:
                lock.heartbeat()
            now_dt = _sequence_value(now_sequence, index)
            status = _sequence_value(status_sequence, index)
            result = run_auto_cycle(
                state=state,
                force_scan=force_scan,
                status_override=status,
                trading_day_override=trading_day_override,
                now_override=now_dt,
                services=services,
                persist_state=True,
                record_event=True,
                db_path=db_path,
                state_file=state_file,
                control_file=control_file,
                notify=notify,
            )
            reports.append(result)
            state = AutoTraderState(**result["state"])
            _write_log(log_file, result["report"])
            if state.last_error:
                errors.append(state.last_error)
            if index < cycles - 1 and interval > 0:
                if lock:
                    lock.heartbeat()
                sleep_func(interval)

        report = None
        closure = None
        if save_report:
            from review.ai_trader_report import generate_ai_trader_report
            report_func = report_func or generate_ai_trader_report
            report = report_func(
                days=report_days,
                end_date=report_end_date,
                db_path=db_path,
                output_dir=report_output_dir,
                save=True,
                control_file=control_file,
            )
            from scheduler.closure_check import run_closure_check
            last_cycle = reports[-1] if reports else {}
            closure = run_closure_check(
                date=report.get("end_date"),
                report=report,
                status_override=last_cycle.get("status"),
                trading_day_override=trading_day_override,
            )

        ended_at = datetime.now()
        if report:
            metrics = report.get("metrics") or {}
            paths = report.get("paths") or {}
            _write_log(
                log_file,
                (
                    "observe_report "
                    f"cycles={metrics.get('auto_cycle_count', 0)} "
                    f"closed_loop_days={metrics.get('closed_loop_days', 0)} "
                    f"trades={metrics.get('trade_count', 0)} "
                    f"lessons={metrics.get('lesson_count', 0)} "
                    f"errors={metrics.get('error_events', 0)} "
                    f"closure={((closure or {}).get('overall') or {}).get('level', '')} "
                    f"markdown={paths.get('markdown', '')}"
                ),
            )
        _write_log(
            log_file,
            (
                f"===== {ended_at.strftime('%Y-%m-%d %H:%M:%S')} END exit=0 "
                f"cycles={len(reports)} errors={len(errors)} ====="
            ),
        )
        return {
            "status": "ok" if not errors else "error",
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "cycles": len(reports),
            "errors": errors,
            "log_file": log_file,
            "evidence_report": report,
            "closure_check": closure,
            "last_report": reports[-1]["report"] if reports else "",
            "reports": reports,
        }
    except Exception as e:
        ended_at = datetime.now()
        _write_log(
            log_file,
            f"===== {ended_at.strftime('%Y-%m-%d %H:%M:%S')} END exit=1 error={e} =====",
        )
        raise
    finally:
        if lock:
            lock.release()


def format_paper_observer(result: Dict) -> str:
    """格式化正式模拟盘观察结果"""
    evidence = result.get("evidence_report") or {}
    closure = result.get("closure_check") or {}
    metrics = evidence.get("metrics") or {}
    paths = evidence.get("paths") or {}
    lines = [
        "=" * 60,
        "正式模拟盘观察运行",
        "=" * 60,
        f"状态: {result.get('status', '')}",
        f"开始: {result.get('started_at', '')}",
        f"结束: {result.get('ended_at', '')}",
        f"轮数: {result.get('cycles', 0)}",
        f"日志: {result.get('log_file', '')}",
    ]
    if evidence:
        lines.extend([
            "-" * 60,
            "正式模拟盘证据:",
            f"报告周期: {evidence.get('start_date', '')} 至 {evidence.get('end_date', '')}",
            (
                f"自动循环{metrics.get('auto_cycle_count', 0)}轮，"
                f"完整闭环日{metrics.get('closed_loop_days', 0)}/{metrics.get('days', 0)}天，"
                f"异常{metrics.get('error_events', 0)}条"
            ),
            (
                f"扫描{metrics.get('scan_count', 0)}次，"
                f"执行{metrics.get('execute_count', 0)}次，"
                f"成交{metrics.get('trade_count', 0)}笔，"
                f"教训{metrics.get('lesson_count', 0)}条"
            ),
            (
                f"执行审计: 计划{metrics.get('order_audit_count', 0)}笔，"
                f"成交{metrics.get('order_filled_count', 0)}笔，"
                f"阻断/跳过{metrics.get('order_blocked_count', 0)}笔，"
                f"失败{metrics.get('order_failed_count', 0)}笔"
            ),
        ])
        if paths.get("markdown"):
            lines.append(f"报告文件: {paths['markdown']}")
        if paths.get("json"):
            lines.append(f"JSON文件: {paths['json']}")
    if closure:
        overall = closure.get("overall") or {}
        next_action = closure.get("next_action") or {}
        lines.extend([
            "-" * 60,
            "日内闭环缺口:",
            f"日期: {closure.get('date', '')} 状态: {closure.get('market_status', '')} level={overall.get('level', '')}",
        ])
        for item in closure.get("items", []):
            if item.get("severity") != "ok":
                lines.append(f"[{item.get('severity', '').upper()}] {item.get('name', '')} - {item.get('detail', '')}")
        if next_action:
            lines.append(f"下一步: {next_action.get('command', '')}")
    errors = result.get("errors") or []
    if errors:
        lines.append(f"异常: {len(errors)}条")
        for error in errors[:5]:
            lines.append(f"  - {error}")
    if result.get("last_report"):
        lines.extend(["-" * 60, "最后一轮:", result["last_report"]])
    lines.append("=" * 60)
    return "\n".join(lines)
