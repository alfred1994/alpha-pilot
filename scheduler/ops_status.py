"""
自动盯盘运维状态汇总
====================================================================
把健康检查、Watchdog、控制开关和AI交易员报告聚合成一个只读状态页。
该模块不触发行情、LLM或交易接口，适合人工巡检和计划任务监控。
====================================================================
"""
from datetime import datetime
from scheduler.market_calendar import _now_bj
from typing import Dict, List

from review.ai_trader_report import generate_ai_trader_report
from scheduler.closure_check import run_closure_check
from scheduler.control import get_auto_control_state
from scheduler.health import format_health_report, run_health_check
from scheduler.watchdog import format_watchdog_report, run_auto_watchdog


def _health_required_failed(items) -> list:
    """提取必需健康检查失败项"""
    return [item.name for item in items if (not item.ok and item.required)]


def _health_optional_failed(items) -> list:
    """提取可选健康检查降级项"""
    return [item.name for item in items if (not item.ok and not item.required)]


def _watchdog_by_severity(items, severity: str) -> list:
    """按严重级别提取Watchdog检查项名称"""
    return [item.name for item in items if item.severity == severity]


def _overall_level(health_required: list, watchdog_critical: list,
                   watchdog_warn: list, health_optional: list,
                   paused: bool) -> str:
    """计算总体状态等级"""
    if health_required or watchdog_critical:
        return "critical"
    if paused or watchdog_warn or health_optional:
        return "warn"
    return "ok"


def _add_action(actions: List[Dict], severity: str, title: str,
                command: str = "", detail: str = ""):
    """添加下一步动作建议"""
    actions.append({
        "severity": severity,
        "title": title,
        "command": command,
        "detail": detail,
    })


def _build_next_actions(
    *,
    health_required: list,
    health_optional: list,
    watchdog_critical: list,
    watchdog_warn: list,
    paused: bool,
    report: Dict,
    closure_check: Dict = None,
    platform: str = None,
) -> List[Dict]:
    """根据当前运维状态生成可执行下一步动作"""
    actions: List[Dict] = []
    metrics = (report or {}).get("metrics") or {}
    closure_action = (closure_check or {}).get("next_action") or {}
    closure_level = ((closure_check or {}).get("overall") or {}).get("level")

    if health_required:
        _add_action(
            actions,
            "critical",
            "先修复必需健康检查",
            "python main.py --health",
            f"失败项: {', '.join(health_required)}",
        )

    if "交易通道" in watchdog_critical:
        _add_action(
            actions,
            "critical",
            "保持模拟盘交易通道",
            "$env:BROKER_MODE='paper'; python main.py --watchdog",
            "当前阶段不应接入实盘交易。",
        )

    if "自动事件读取" in watchdog_critical:
        _add_action(
            actions,
            "critical",
            "检查SQLite事件库",
            "python main.py --health",
            "auto_events读取失败会让Watchdog无法判断闭环状态。",
        )

    auto_runtime_critical = [
        name for name in watchdog_critical
        if name in ("自动盘锁", "自动循环新鲜度", "自动盯盘状态")
    ]
    if auto_runtime_critical:
        if platform == "linux":
            cmd = "systemctl --user restart quant-pilot-auto.service"
        else:
            cmd = "powershell -ExecutionPolicy Bypass -File data\\\\task_scripts\\\\restart_auto.ps1"
        _add_action(
            actions,
            "critical",
            "重启自动盘长驻任务",
            cmd,
            f"长驻自动盘异常: {', '.join(auto_runtime_critical)}；若计划任务未安装，先用 --paper-observe 手动观察。",
        )

    recoverable = [
        name for name in watchdog_critical
        if name not in ("交易通道", "自动事件读取", "自动盘锁", "自动循环新鲜度", "自动盯盘状态")
    ]
    if recoverable:
        _add_action(
            actions,
            "critical",
            "触发Doctor自愈",
            "python main.py --doctor --force-scan",
            f"可尝试自愈: {', '.join(recoverable)}",
        )

    if paused:
        _add_action(
            actions,
            "warn",
            "恢复盘中自动交易动作",
            "python main.py --auto-resume",
            "当前控制开关处于暂停，盘中扫描/执行/止损巡检会被跳过。",
        )

    if closure_action and closure_level in ("critical", "warn"):
        _add_action(
            actions,
            closure_action.get("severity", closure_level),
            closure_action.get("title", "补齐日内闭环缺口"),
            closure_action.get("command", ""),
            closure_action.get("detail", ""),
        )

    if "自动盘锁" in watchdog_warn and not auto_runtime_critical:
        _add_action(
            actions,
            "warn",
            "启动自动盘长驻进程",
            "python main.py --paper-observe --observe-cycles 5",
            "未发现长驻锁；先用有限轮正式模拟盘观察积累证据，准备无人值守时再启动 --auto 或安装计划任务。",
        )

    other_watchdog_warn = [name for name in watchdog_warn if name != "自动盘锁"]
    if other_watchdog_warn:
        _add_action(
            actions,
            "warn",
            "复查Watchdog告警",
            "python main.py --watchdog",
            f"warn项: {', '.join(other_watchdog_warn)}",
        )

    if metrics.get("auto_cycle_count", 0) == 0:
        _add_action(
            actions,
            "warn",
            "启动模拟盘观察",
            "python main.py --paper-observe --observe-cycles 5",
            "最近报告没有正式自动循环证据；观察命令会写入正式库和 logs/paper_observe.log。",
        )
    elif metrics.get("closed_loop_days", 0) == 0:
        _add_action(
            actions,
            "warn",
            "积累正式模拟盘闭环证据",
            "python main.py --paper-observe --observe-cycles 5",
            "正式库已有循环记录，但还没有真实交易日完整闭环；继续覆盖盘中和盘后复盘窗口。",
        )

    if health_optional:
        _add_action(
            actions,
            "info",
            "补齐可选数据源能力",
            "python main.py --health",
            f"降级项: {', '.join(health_optional)}",
        )

    if not actions:
        _add_action(
            actions,
            "ok",
            "保持无人值守观察",
            "python main.py --ops-status --report-days 5",
            "当前基础状态正常，继续跟踪闭环日、教训和自适应参数。",
        )
    return actions


def run_ops_status(
    *,
    report_days: int = 5,
    report_end_date: str = None,
    db_path: str = None,
    output_dir: str = None,
    save_report: bool = True,
    now: datetime = None,
    status_override: str = None,
    trading_day_override: bool = None,
    state_file: str = None,
    lock_file: str = None,
    control_file: str = None,
    max_loop_lag_sec: int = None,
    max_scan_lag_sec: int = None,
    max_stop_lag_sec: int = None,
) -> Dict:
    """
    生成自动盯盘运维状态

    Args:
        report_days: AI交易员报告回看天数
        report_end_date: AI交易员报告结束日期
        db_path/output_dir: SQLite和报告输出目录，便于演练隔离
        save_report: 是否保存AI交易员Markdown/JSON报告
        now/status_override/trading_day_override/state_file/control_file: 测试注入项

    Returns:
        汇总状态字典，包含health/watchdog/report/control/overall
    """
    now = now or _now_bj()
    health_items = run_health_check(db_path=db_path, control_file=control_file)
    watchdog_items = run_auto_watchdog(
        now=now,
        status_override=status_override,
        trading_day_override=trading_day_override,
        state_file=state_file,
        lock_file=lock_file,
        control_file=control_file,
        db_path=db_path,
        max_loop_lag_sec=max_loop_lag_sec,
        max_scan_lag_sec=max_scan_lag_sec,
        max_stop_lag_sec=max_stop_lag_sec,
    )
    report = generate_ai_trader_report(
        days=report_days,
        end_date=report_end_date,
        db_path=db_path,
        output_dir=output_dir,
        save=save_report,
        control_file=control_file,
    )
    closure = run_closure_check(
        date=report_end_date,
        now=now,
        db_path=db_path,
        control_file=control_file,
        report=(
            report
            if report_end_date is None or report.get("end_date") == (report_end_date or report.get("end_date"))
            else None
        ),
        status_override=status_override,
        trading_day_override=trading_day_override,
    )
    control = get_auto_control_state(control_file=control_file)

    health_required = _health_required_failed(health_items)
    health_optional = _health_optional_failed(health_items)
    watchdog_critical = _watchdog_by_severity(watchdog_items, "critical")
    watchdog_warn = _watchdog_by_severity(watchdog_items, "warn")
    paused = bool(control.get("paused"))
    level = _overall_level(
        health_required,
        watchdog_critical,
        watchdog_warn,
        health_optional,
        paused,
    )
    next_actions = _build_next_actions(
        health_required=health_required,
        health_optional=health_optional,
        watchdog_critical=watchdog_critical,
        watchdog_warn=watchdog_warn,
        paused=paused,
        report=report,
        closure_check=closure,
    )

    return {
        "generated_at": now.isoformat(),
        "overall": {
            "level": level,
            "ok": level == "ok",
            "health_required_failed": health_required,
            "health_optional_failed": health_optional,
            "watchdog_critical": watchdog_critical,
            "watchdog_warn": watchdog_warn,
            "paused": paused,
        },
        "control": control,
        "health_items": health_items,
        "watchdog_items": watchdog_items,
        "report": report,
        "closure_check": closure,
        "next_actions": next_actions,
    }


def format_ops_status(status: Dict, include_sections: bool = False) -> str:
    """格式化自动盯盘运维状态"""
    overall = status.get("overall") or {}
    control = status.get("control") or {}
    report = status.get("report") or {}
    closure = status.get("closure_check") or {}
    metrics = report.get("metrics") or {}
    next_params = report.get("next_trade_params") or {}
    current_params = next_params.get("current") or {}
    paths = report.get("paths") or {}

    level_label = {
        "ok": "OK",
        "warn": "WARN",
        "critical": "CRITICAL",
    }.get(overall.get("level"), "UNKNOWN")
    control_status = "暂停" if control.get("paused") else "运行"

    lines = [
        "=" * 60,
        "自动盯盘运维状态",
        "=" * 60,
        f"生成时间: {status.get('generated_at', '')}",
        f"总体状态: {level_label}",
        f"控制开关: {control_status} reason={control.get('reason', '')}",
        f"模拟报告: {report.get('start_date', '')} 至 {report.get('end_date', '')}",
        f"闭环概览: 自动循环{metrics.get('auto_cycle_count', 0)}轮，完整闭环日{metrics.get('closed_loop_days', 0)}/{metrics.get('days', 0)}天，异常{metrics.get('error_events', 0)}条",
        f"状态归因: 无循环{metrics.get('no_loop_days', 0)}天，仅复盘{metrics.get('review_only_days', 0)}天，暂停{metrics.get('paused_days', 0)}天，休市{metrics.get('holiday_days', 0)}天，缺扫描{metrics.get('missing_scan_days', 0)}天，缺执行{metrics.get('missing_execute_days', 0)}天，缺复盘{metrics.get('missing_review_days', 0)}天",
        f"盘中观察: 轻量看盘{metrics.get('watch_count', 0)}次，疑似踏空{metrics.get('missed_opportunity_count', 0)}次，救援扫描{metrics.get('rescue_scan_count', 0)}次",
        f"下一轮参数: {next_params.get('current_regime', 'sideways')} Top{current_params.get('top_k', '-')} 最低分{current_params.get('min_score', '-')} 单票上限{float(current_params.get('max_weight') or 0):.1%}",
        f"交易学习: LLM决策{metrics.get('llm_decision_count', 0)}条，模拟成交{metrics.get('trade_count', 0)}笔，教训{metrics.get('lesson_count', 0)}条",
        f"执行审计: 计划{metrics.get('order_audit_count', 0)}笔，成交{metrics.get('order_filled_count', 0)}笔，阻断/跳过{metrics.get('order_blocked_count', 0)}笔，失败{metrics.get('order_failed_count', 0)}笔",
    ]
    if closure:
        closure_overall = closure.get("overall") or {}
        closure_daily = closure.get("daily") or {}
        lines.append(
            f"今日闭环缺口: {closure.get('date', '')} {closure_overall.get('level', '').upper()} "
            f"诊断={closure_daily.get('diagnosis', '-')}"
        )

    if paths.get("markdown"):
        lines.append(f"报告文件: {paths['markdown']}")
    if paths.get("json"):
        lines.append(f"JSON文件: {paths['json']}")

    required = overall.get("health_required_failed") or []
    optional = overall.get("health_optional_failed") or []
    critical = overall.get("watchdog_critical") or []
    warn = overall.get("watchdog_warn") or []

    lines.extend(["-" * 60, "关键问题:"])
    if not any([required, critical, warn, optional, control.get("paused")]):
        lines.append("  无，自动盯盘基础状态正常。")
    if required:
        lines.append(f"  必需健康检查失败: {', '.join(required)}")
    if critical:
        lines.append(f"  Watchdog critical: {', '.join(critical)}")
    if warn:
        lines.append(f"  Watchdog warn: {', '.join(warn)}")
    if optional:
        lines.append(f"  可选能力降级: {', '.join(optional)}")
    if control.get("paused"):
        lines.append("  当前处于暂停状态，盘中交易动作会被跳过。")

    lines.extend(["-" * 60, "建议动作:"])
    for action in status.get("next_actions", []):
        label = action.get("severity", "info").upper()
        lines.append(f"  [{label}] {action.get('title', '')}")
        if action.get("detail"):
            lines.append(f"    说明: {action['detail']}")
        if action.get("command"):
            lines.append(f"    命令: {action['command']}")

    if include_sections:
        lines.extend([
            "",
            format_health_report(status.get("health_items", [])),
            "",
            format_watchdog_report(status.get("watchdog_items", [])),
        ])

    lines.append("=" * 60)
    return "\n".join(lines)
