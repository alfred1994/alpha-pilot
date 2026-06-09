"""
模拟盘AI交易员就绪检查
====================================================================
用于启动无人值守模拟盘前做一页式门禁：
  - 基础健康检查是否通过
  - 交易通道是否保持 paper
  - 最近演练是否证明闭环可跑通
  - Windows计划任务或Linux systemd用户任务是否就绪
  - 正式模拟盘报告是否已有运行证据

该模块只读本地状态和SQLite，不触发行情、LLM或交易接口。
====================================================================
"""
from dataclasses import dataclass
from datetime import datetime
import os
from typing import Dict, List, Optional

from config import BROKER_MODE
from review.ai_trader_report import generate_ai_trader_report
from scheduler.health import HealthItem, run_health_check
from scheduler.rehearsal import REHEARSAL_DB, REHEARSAL_REPORT_DIR
from scheduler.windows_tasks import UnattendedItem, run_unattended_status


@dataclass
class ReadinessItem:
    """模拟盘就绪检查项"""
    name: str
    ok: bool
    severity: str = "ok"  # ok/warn/critical
    detail: str = ""
    suggestion: str = ""


def _item(name: str, ok: bool, detail: str,
          severity: str = None, suggestion: str = "") -> ReadinessItem:
    """构造就绪检查项"""
    if severity is None:
        severity = "ok" if ok else "warn"
    return ReadinessItem(name=name, ok=ok, severity=severity, detail=detail, suggestion=suggestion)


def _required_health_failed(items: List[HealthItem]) -> List[str]:
    """提取必需健康检查失败项"""
    return [item.name for item in items if item.required and not item.ok]


def _summarise_report(report: Dict) -> Dict:
    """提取报告关键指标"""
    metrics = report.get("metrics") or {}
    return {
        "days": metrics.get("days", 0),
        "auto_cycle_count": metrics.get("auto_cycle_count", 0),
        "closed_loop_days": metrics.get("closed_loop_days", 0),
        "trade_count": metrics.get("trade_count", 0),
        "lesson_count": metrics.get("lesson_count", 0),
        "adaptive_available": bool((report.get("adaptive") or {}).get("available")),
        "error_events": metrics.get("error_events", 0),
    }


def _load_rehearsal_report(days: int, end_date: str = None,
                           db_path: str = None) -> Optional[Dict]:
    """读取最近演练报告，缺失时返回None"""
    db_path = db_path or REHEARSAL_DB
    if not os.path.exists(db_path):
        return None
    if end_date is None:
        try:
            from data.database import Database
            with Database(db_path=db_path) as db:
                row = db.conn.execute(
                    "SELECT MAX(date) AS max_date FROM auto_events"
                ).fetchone()
                end_date = row["max_date"] if row and row["max_date"] else None
        except Exception:
            end_date = None
    return generate_ai_trader_report(
        days=days,
        end_date=end_date,
        db_path=db_path,
        output_dir=REHEARSAL_REPORT_DIR,
        save=False,
    )


def _check_rehearsal(report: Optional[Dict], min_days: int) -> ReadinessItem:
    """检查演练闭环证据"""
    if not report:
        return _item(
            "连续演练闭环",
            False,
            "未发现演练数据库",
            "critical",
            "运行 python main.py --auto-rehearse --rehearsal-days 3。",
        )

    summary = _summarise_report(report)
    ok = (
        summary["closed_loop_days"] >= min_days
        and summary["trade_count"] >= min_days
        and summary["lesson_count"] >= min_days
        and summary["adaptive_available"]
        and summary["error_events"] == 0
    )
    detail = (
        f"闭环{summary['closed_loop_days']}/{summary['days']}天 "
        f"循环{summary['auto_cycle_count']}轮 "
        f"成交{summary['trade_count']}笔 "
        f"教训{summary['lesson_count']}条 "
        f"自适应={'有' if summary['adaptive_available'] else '无'} "
        f"异常{summary['error_events']}条"
    )
    return _item(
        "连续演练闭环",
        ok,
        detail,
        "critical" if not ok else "ok",
        "演练必须证明盘前/盘中/盘后、成交、复盘、教训和参数纠偏都能跑通。",
    )


def _check_formal_report(report: Dict) -> ReadinessItem:
    """检查正式模拟盘运行证据"""
    summary = _summarise_report(report)
    if summary["auto_cycle_count"] == 0:
        return _item(
            "正式模拟盘证据",
            False,
            "最近报告无自动循环记录",
            "warn",
            "先运行 python main.py --paper-observe --observe-cycles 5 积累正式模拟盘证据。",
        )
    ok = summary["closed_loop_days"] > 0 and summary["error_events"] == 0
    detail = (
        f"闭环{summary['closed_loop_days']}/{summary['days']}天 "
        f"循环{summary['auto_cycle_count']}轮 "
        f"成交{summary['trade_count']}笔 "
        f"教训{summary['lesson_count']}条 "
        f"异常{summary['error_events']}条"
    )
    return _item(
        "正式模拟盘证据",
        ok,
        detail,
        "ok" if ok else "warn",
        "当前可先运行；若未形成完整闭环，继续用 --paper-observe 或 --auto 覆盖真实交易日。",
    )


def _check_unattended(items: List[UnattendedItem], platform: str = "windows") -> ReadinessItem:
    """汇总无人值守巡检结果"""
    critical = [i.name for i in items if i.severity == "critical"]
    warn = [i.name for i in items if i.severity == "warn"]
    if platform == "linux":
        fix_cmd = "运行 python3 main.py --linux-tasks 修复脚本，再执行 --linux-unattended-status。"
        warn_cmd = "执行 bash data/linux_tasks/install_systemd_user.sh，并观察 logs 最近运行结果。"
    else:
        fix_cmd = "运行 python main.py --windows-tasks 修复脚本，再执行 --unattended-status。"
        warn_cmd = "用管理员 PowerShell 安装计划任务，并观察 logs 最近运行结果。"
    if critical:
        return _item(
            "无人值守配置",
            False,
            f"critical={len(critical)} warn={len(warn)}: {', '.join(critical[:4])}",
            "critical",
            fix_cmd,
        )
    if warn:
        return _item(
            "无人值守配置",
            False,
            f"warn={len(warn)}: {', '.join(warn[:4])}",
            "warn",
            warn_cmd,
        )
    return _item("无人值守配置", True, "脚本、计划任务和最近日志正常", "ok")


def run_paper_readiness(
    *,
    report_days: int = 5,
    report_end_date: str = None,
    rehearsal_days: int = 3,
    rehearsal_end_date: str = None,
    db_path: str = None,
    rehearsal_db_path: str = None,
    task_output_dir: str = None,
    task_prefix: str = "QuantPilot",
    service_prefix: str = "quant-pilot",
    unattended_platform: str = "windows",
    control_file: str = None,
    project_dir: str = None,
    health_items: List[HealthItem] = None,
    unattended_items: List[UnattendedItem] = None,
    formal_report: Dict = None,
    rehearsal_report: Dict = None,
) -> Dict:
    """
    执行模拟盘AI交易员就绪检查

    Args中的 health_items/unattended_items/formal_report/rehearsal_report 用于测试注入。
    """
    generated_at = datetime.now().isoformat()
    items: List[ReadinessItem] = []

    health_items = health_items if health_items is not None else run_health_check(
        db_path=db_path,
        control_file=control_file,
    )
    health_failed = _required_health_failed(health_items)
    items.append(_item(
        "基础健康",
        not health_failed,
        "必需项正常" if not health_failed else f"失败: {', '.join(health_failed)}",
        "critical" if health_failed else "ok",
        "先修复必需依赖、数据库或交易通道。",
    ))

    paper_ok = (BROKER_MODE or "paper").lower() == "paper"
    items.append(_item(
        "模拟盘交易通道",
        paper_ok,
        f"BROKER_MODE={BROKER_MODE}",
        "critical" if not paper_ok else "ok",
        "当前阶段必须保持 BROKER_MODE=paper。",
    ))

    if unattended_items is None:
        platform = (unattended_platform or "windows").lower()
        if platform == "linux":
            from scheduler.linux_tasks import run_linux_unattended_status

            unattended_items = run_linux_unattended_status(
                project_dir=project_dir,
                output_dir=task_output_dir,
                service_prefix=service_prefix,
            )
        else:
            unattended_items = run_unattended_status(
                project_dir=project_dir,
                output_dir=task_output_dir,
                task_prefix=task_prefix,
            )
    items.append(_check_unattended(unattended_items, unattended_platform))

    formal_report = formal_report if formal_report is not None else generate_ai_trader_report(
        days=report_days,
        end_date=report_end_date,
        db_path=db_path,
        save=False,
        control_file=control_file,
    )
    items.append(_check_formal_report(formal_report))

    rehearsal_report = (
        rehearsal_report
        if rehearsal_report is not None
        else _load_rehearsal_report(
            days=rehearsal_days,
            end_date=rehearsal_end_date,
            db_path=rehearsal_db_path,
        )
    )
    items.append(_check_rehearsal(rehearsal_report, rehearsal_days))

    critical = [i.name for i in items if i.severity == "critical"]
    warn = [i.name for i in items if i.severity == "warn"]
    return {
        "generated_at": generated_at,
        "items": items,
        "health_items": health_items,
        "unattended_items": unattended_items,
        "formal_report": formal_report,
        "rehearsal_report": rehearsal_report,
        "overall": {
            "ready": not critical,
            "fully_unattended_ready": not critical and not warn,
            "critical": critical,
            "warn": warn,
        },
    }


def format_paper_readiness(result: Dict) -> str:
    """格式化模拟盘就绪检查报告"""
    overall = result.get("overall") or {}
    lines = [
        "=" * 60,
        "模拟盘AI交易员就绪检查",
        "=" * 60,
        f"生成时间: {result.get('generated_at', '')}",
    ]
    for item in result.get("items", []):
        label = item.severity.upper() if item.severity != "ok" else "OK"
        lines.append(f"[{label}] {item.name} - {item.detail}")
        if not item.ok and item.suggestion:
            lines.append(f"  建议: {item.suggestion}")

    critical = overall.get("critical") or []
    warn = overall.get("warn") or []
    lines.append("-" * 60)
    if critical:
        lines.append(f"结论: 暂不建议启动无人值守模拟盘，critical={len(critical)} warn={len(warn)}")
    elif warn:
        lines.append(f"结论: 可手动启动模拟盘观察，但无人值守仍有 warn={len(warn)}")
    else:
        lines.append("结论: 模拟盘AI交易员已具备无人值守运行条件")
    lines.append("=" * 60)
    return "\n".join(lines)
