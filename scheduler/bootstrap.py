"""
模拟盘AI交易员启动引导
====================================================================
把上线前最容易漏掉的准备动作串成一个保守的一键流程：
  1. 生成Windows计划任务或Linux systemd用户任务脚本，并强制默认模拟盘
  2. 运行隔离数据库连续演练，验证闭环和纠偏
  3. 执行模拟盘就绪门禁，输出可启动/待修复结论

本模块不会安装Windows计划任务，也不会连接真实交易通道。
====================================================================
"""
import os
from datetime import datetime
from scheduler.market_calendar import _now_bj
from typing import Callable, Dict

from scheduler.readiness import format_paper_readiness, run_paper_readiness
from scheduler.rehearsal import run_auto_rehearsal
from scheduler.windows_tasks import generate_windows_task_scripts


def run_paper_bootstrap(
    *,
    project_dir: str = None,
    task_output_dir: str = None,
    python_cmd: str = "python",
    task_prefix: str = "QuantPilot",
    service_prefix: str = "quant-pilot",
    unattended_platform: str = "windows",
    hermes_env_file: str = "$HOME/.hermes/.env",
    report_days: int = 5,
    report_end_date: str = None,
    rehearsal_days: int = 5,
    rehearsal_start_date: str = None,
    generate_scripts_func: Callable = None,
    run_rehearsal_func: Callable = None,
    run_readiness_func: Callable = None,
) -> Dict:
    """
    执行模拟盘上线前引导

    Args中的三个函数参数用于测试注入。实际运行时只触发脚本生成、
    隔离演练和只读就绪检查，不安装系统任务、不触发实盘交易。
    """
    project_dir = os.path.abspath(project_dir or os.getcwd())
    platform = (unattended_platform or "windows").lower()
    if generate_scripts_func is None and platform == "linux":
        from scheduler.linux_tasks import generate_linux_task_scripts

        generate_scripts_func = generate_linux_task_scripts
    else:
        generate_scripts_func = generate_scripts_func or generate_windows_task_scripts
    run_rehearsal_func = run_rehearsal_func or run_auto_rehearsal
    run_readiness_func = run_readiness_func or run_paper_readiness

    generated_at = _now_bj().isoformat()
    if platform == "linux":
        paths = generate_scripts_func(
            project_dir=project_dir,
            output_dir=task_output_dir,
            python_cmd=python_cmd,
            service_prefix=service_prefix,
            report_days=report_days,
            hermes_env_file=hermes_env_file,
        )
    else:
        paths = generate_scripts_func(
            project_dir=project_dir,
            output_dir=task_output_dir,
            python_cmd=python_cmd,
            task_prefix=task_prefix,
            report_days=report_days,
        )

    rehearsal = run_rehearsal_func(
        days=rehearsal_days,
        start_date=rehearsal_start_date,
        save_report=True,
    )
    rehearsal_report = (rehearsal or {}).get("report") or {}

    readiness = run_readiness_func(
        report_days=report_days,
        report_end_date=report_end_date,
        rehearsal_days=rehearsal_days,
        task_output_dir=task_output_dir,
        task_prefix=task_prefix,
        service_prefix=service_prefix,
        unattended_platform=platform,
        project_dir=project_dir,
        rehearsal_report=rehearsal_report,
    )
    critical = (readiness.get("overall") or {}).get("critical") or []
    warn = (readiness.get("overall") or {}).get("warn") or []

    return {
        "generated_at": generated_at,
        "project_dir": project_dir,
        "unattended_platform": platform,
        "script_paths": paths,
        "script_dir": os.path.dirname(paths.get("install", "")) if paths else "",
        "rehearsal": rehearsal,
        "readiness": readiness,
        "overall": {
            "ready": not critical,
            "fully_unattended_ready": not critical and not warn,
            "critical": critical,
            "warn": warn,
        },
        "errors": critical,
    }


def _rehearsal_summary(rehearsal: Dict) -> str:
    """格式化演练摘要"""
    report = (rehearsal or {}).get("report") or {}
    metrics = report.get("metrics") or {}
    return (
        f"闭环{metrics.get('closed_loop_days', 0)}/{metrics.get('days', 0)}天 "
        f"循环{metrics.get('auto_cycle_count', 0)}轮 "
        f"成交{metrics.get('trade_count', 0)}笔 "
        f"教训{metrics.get('lesson_count', 0)}条"
    )


def format_paper_bootstrap(result: Dict) -> str:
    """格式化模拟盘启动引导报告"""
    overall = result.get("overall") or {}
    critical = overall.get("critical") or []
    warn = overall.get("warn") or []
    paths = result.get("script_paths") or {}
    rehearsal = result.get("rehearsal") or {}

    lines = [
        "=" * 60,
        "模拟盘AI交易员启动引导",
        "=" * 60,
        f"生成时间: {result.get('generated_at', '')}",
        f"项目目录: {result.get('project_dir', '')}",
        f"脚本目录: {result.get('script_dir', '')}",
        f"演练数据库: {rehearsal.get('db_path', '')}",
        f"演练摘要: {_rehearsal_summary(rehearsal)}",
    ]
    if paths.get("install"):
        label = "systemd用户任务安装脚本" if result.get("unattended_platform") == "linux" else "计划任务安装脚本"
        lines.append(f"{label}: {paths['install']}")
    lines.extend([
        "-" * 60,
        "就绪门禁:",
        format_paper_readiness(result.get("readiness") or {}),
        "-" * 60,
    ])
    if critical:
        lines.append(f"结论: 仍有critical={len(critical)}，先修复后再启动模拟盘。")
    elif warn:
        lines.append(f"结论: 可手动启动模拟盘观察；无人值守仍有warn={len(warn)}。")
        if result.get("unattended_platform") == "linux":
            lines.append("下一步: 先运行 python3 main.py --paper-observe --observe-cycles 5；需要无人值守时再安装 install_systemd_user.sh。")
        else:
            lines.append("下一步: 先运行 python main.py --paper-observe --observe-cycles 5；需要无人值守时再安装 install_tasks.ps1。")
    else:
        lines.append("结论: 已具备无人值守模拟盘运行条件。")
    lines.append("保护: 本流程不安装计划任务，不连接实盘，默认保持BROKER_MODE=paper。")
    lines.append("=" * 60)
    return "\n".join(lines)
