"""
Windows无人值守任务脚本生成器
====================================================================
为模拟盘AI交易员生成一组 PowerShell 脚本：
  - run_auto.ps1: 启动自动盯盘循环
  - restart_auto.ps1: Watchdog发现停滞时重启自动盘
  - run_doctor.ps1: 执行Watchdog巡检与自愈
  - run_report.ps1: 生成最近N天试运行报告
  - run_status.ps1: 生成一页式运维状态
  - install_tasks.ps1: 注册Windows计划任务
  - uninstall_tasks.ps1: 删除Windows计划任务

本模块只生成脚本，不直接修改系统计划任务。
====================================================================
"""
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List

from config import DATA_DIR


DEFAULT_OUTPUT_DIR = os.path.join(DATA_DIR, "task_scripts")
DEFAULT_TASK_PREFIX = "AlphaPilot"
TASK_NAMES = ["Auto", "Auto-Restart", "Doctor", "Report", "Status"]
SCRIPT_KEYS = ["run_auto", "restart_auto", "run_doctor", "run_report", "run_status", "install", "uninstall"]
LOG_NAMES = {
    "Auto": "auto.log",
    "Auto-Restart": "auto_restart.log",
    "Doctor": "doctor.log",
    "Report": "ai_report.log",
    "Status": "ops_status.log",
}


@dataclass
class TaskScriptConfig:
    """Windows计划任务脚本配置"""
    project_dir: str
    output_dir: str = DEFAULT_OUTPUT_DIR
    python_cmd: str = "python"
    task_prefix: str = DEFAULT_TASK_PREFIX
    report_days: int = 5
    auto_start: str = "08:45"
    doctor_start: str = "09:00"
    doctor_interval_minutes: int = 5
    doctor_duration_hours: int = 7
    restart_start: str = "09:05"
    restart_interval_minutes: int = 10
    restart_duration_hours: int = 7
    report_time: str = "15:40"
    status_time: str = "15:50"


@dataclass
class UnattendedItem:
    """无人值守检查项"""
    name: str
    ok: bool
    severity: str = "ok"  # ok/warn/critical
    detail: str = ""
    suggestion: str = ""


def _ps_quote(value: str) -> str:
    """PowerShell单引号转义"""
    return "'" + str(value).replace("'", "''") + "'"


def _runner_script(project_dir: str, python_cmd: str, args: str,
                   log_name: str) -> str:
    """生成单个运行脚本"""
    log_path = os.path.join(project_dir, "logs", log_name)
    return f"""# AlphaPilot 自动生成脚本
$ErrorActionPreference = 'Stop'
$ProjectDir = {_ps_quote(project_dir)}
$PythonCmd = {_ps_quote(python_cmd)}
$LogFile = {_ps_quote(log_path)}

New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
Set-Location $ProjectDir
$env:BROKER_MODE = 'paper'

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $LogFile -Value "===== $stamp START {args} ====="
& $PythonCmd main.py {args} *>> $LogFile
$exitCode = $LASTEXITCODE
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $LogFile -Value "===== $stamp END exit=$exitCode ====="
exit $exitCode
"""


def _restart_auto_script(project_dir: str, python_cmd: str,
                         task_prefix: str) -> str:
    """生成自动盘重启脚本"""
    log_path = os.path.join(project_dir, "logs", "auto_restart.log")
    return f"""# AlphaPilot 自动盘重启脚本
$ErrorActionPreference = 'Continue'
$ProjectDir = {_ps_quote(project_dir)}
$PythonCmd = {_ps_quote(python_cmd)}
$TaskPrefix = {_ps_quote(task_prefix)}
$LogFile = {_ps_quote(log_path)}
$AutoTask = "$TaskPrefix-Auto"

New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
Set-Location $ProjectDir
$env:BROKER_MODE = 'paper'

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $LogFile -Value "===== $stamp START restart-auto ====="

$watchdogOutput = & $PythonCmd main.py --watchdog 2>&1
$watchdogExit = $LASTEXITCODE
$watchdogOutput | Add-Content -Path $LogFile
if ($watchdogExit -eq 0) {{
    Add-Content -Path $LogFile -Value "Watchdog OK, no restart needed."
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $LogFile -Value "===== $stamp END exit=0 ====="
    exit 0
}}

$runtimeFault = $false
foreach ($Line in $watchdogOutput) {{
    if ($Line -match '自动盘锁|自动循环新鲜度|自动盯盘状态') {{
        $runtimeFault = $true
        break
    }}
}}
if (-not $runtimeFault) {{
    Add-Content -Path $LogFile -Value "Watchdog failed but no auto-runtime fault, skip restart."
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $LogFile -Value "===== $stamp END exit=$watchdogExit ====="
    exit $watchdogExit
}}

Add-Content -Path $LogFile -Value "Watchdog exit=$watchdogExit, restarting $AutoTask"
$restartExit = 0
try {{
    Stop-ScheduledTask -TaskName $AutoTask -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Start-ScheduledTask -TaskName $AutoTask -ErrorAction Stop
}} catch {{
    $restartExit = 1
    Add-Content -Path $LogFile -Value "Restart failed: $($_.Exception.Message)"
}}

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $LogFile -Value "===== $stamp END exit=$restartExit ====="
exit $restartExit
"""


def _expected_paths(output_dir: str) -> Dict[str, str]:
    """根据输出目录生成脚本路径字典"""
    return {
        "run_auto": os.path.join(output_dir, "run_auto.ps1"),
        "restart_auto": os.path.join(output_dir, "restart_auto.ps1"),
        "run_doctor": os.path.join(output_dir, "run_doctor.ps1"),
        "run_report": os.path.join(output_dir, "run_report.ps1"),
        "run_status": os.path.join(output_dir, "run_status.ps1"),
        "install": os.path.join(output_dir, "install_tasks.ps1"),
        "uninstall": os.path.join(output_dir, "uninstall_tasks.ps1"),
    }


def _install_script(config: TaskScriptConfig, paths: Dict[str, str]) -> str:
    """生成Windows计划任务安装脚本"""
    prefix = config.task_prefix
    days = "Monday,Tuesday,Wednesday,Thursday,Friday"
    return f"""# AlphaPilot Windows计划任务安装脚本
# 请在管理员 PowerShell 中执行：
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\\install_tasks.ps1

$ErrorActionPreference = 'Stop'
$TaskPrefix = {_ps_quote(prefix)}
$WorkDir = {_ps_quote(config.project_dir)}

function Register-QuantTask($Name, $ScriptPath, $Trigger, $Description) {{
    $Action = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`"" `
        -WorkingDirectory $WorkDir
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask `
        -TaskName "$TaskPrefix-$Name" `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description $Description `
        -Force | Out-Null
    Write-Host "registered $TaskPrefix-$Name"
}}

$AutoTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek {days} `
    -At {_ps_quote(config.auto_start)}
$DoctorTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek {days} `
    -At {_ps_quote(config.doctor_start)} `
    -RepetitionInterval (New-TimeSpan -Minutes {config.doctor_interval_minutes}) `
    -RepetitionDuration (New-TimeSpan -Hours {config.doctor_duration_hours})
$RestartTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek {days} `
    -At {_ps_quote(config.restart_start)} `
    -RepetitionInterval (New-TimeSpan -Minutes {config.restart_interval_minutes}) `
    -RepetitionDuration (New-TimeSpan -Hours {config.restart_duration_hours})
$ReportTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek {days} `
    -At {_ps_quote(config.report_time)}
$StatusTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek {days} `
    -At {_ps_quote(config.status_time)}

Register-QuantTask `
    -Name 'Auto' `
    -ScriptPath {_ps_quote(paths['run_auto'])} `
    -Trigger $AutoTrigger `
    -Description 'AlphaPilot 模拟盘自动盯盘循环'
Register-QuantTask `
    -Name 'Doctor' `
    -ScriptPath {_ps_quote(paths['run_doctor'])} `
    -Trigger $DoctorTrigger `
    -Description 'AlphaPilot 自动盯盘Watchdog巡检与自愈'
Register-QuantTask `
    -Name 'Auto-Restart' `
    -ScriptPath {_ps_quote(paths['restart_auto'])} `
    -Trigger $RestartTrigger `
    -Description 'AlphaPilot 自动盯盘长驻进程Watchdog重启'
Register-QuantTask `
    -Name 'Report' `
    -ScriptPath {_ps_quote(paths['run_report'])} `
    -Trigger $ReportTrigger `
    -Description 'AlphaPilot AI交易员模拟盘试运行报告'
Register-QuantTask `
    -Name 'Status' `
    -ScriptPath {_ps_quote(paths['run_status'])} `
    -Trigger $StatusTrigger `
    -Description 'AlphaPilot 自动盯盘一页式运维状态'

Write-Host 'AlphaPilot scheduled tasks installed.'
"""


def _uninstall_script(task_prefix: str) -> str:
    """生成Windows计划任务卸载脚本"""
    return f"""# AlphaPilot Windows计划任务卸载脚本
$ErrorActionPreference = 'Continue'
$TaskPrefix = {_ps_quote(task_prefix)}
foreach ($Name in @('Auto', 'Auto-Restart', 'Doctor', 'Report', 'Status')) {{
    $TaskName = "$TaskPrefix-$Name"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "unregistered $TaskName"
}}
"""


def _query_windows_tasks(task_prefix: str) -> Dict[str, Dict]:
    """读取Windows计划任务状态，失败时抛出异常供上层降级"""
    escaped_prefix = task_prefix.replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        f"$prefix='{escaped_prefix}-';"
        "Get-ScheduledTask | "
        "Where-Object {$_.TaskName -like ($prefix + '*')} | "
        "ForEach-Object {"
        "$i=Get-ScheduledTaskInfo -TaskName $_.TaskName -TaskPath $_.TaskPath;"
        "[PSCustomObject]@{Name=$_.TaskName;State=$_.State;LastRunTime=$i.LastRunTime;LastTaskResult=$i.LastTaskResult}"
        "} | ConvertTo-Json -Compress"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip())
    raw = (proc.stdout or "").strip()
    if not raw:
        return {}
    import json
    data = json.loads(raw)
    rows = data if isinstance(data, list) else [data]
    return {str(row.get("Name", "")): row for row in rows if row.get("Name")}


def generate_windows_task_scripts(
    project_dir: str = None,
    output_dir: str = None,
    python_cmd: str = "python",
    task_prefix: str = DEFAULT_TASK_PREFIX,
    report_days: int = 5,
) -> Dict[str, str]:
    """
    生成Windows无人值守计划任务脚本

    Args:
        project_dir: 项目根目录，默认当前工作目录
        output_dir: 输出目录，默认data/task_scripts
        python_cmd: Python命令
        task_prefix: 计划任务名前缀
        report_days: 报告回看天数

    Returns:
        脚本路径字典
    """
    project_dir = os.path.abspath(project_dir or os.getcwd())
    output_dir = os.path.abspath(output_dir or DEFAULT_OUTPUT_DIR)
    os.makedirs(output_dir, exist_ok=True)

    config = TaskScriptConfig(
        project_dir=project_dir,
        output_dir=output_dir,
        python_cmd=python_cmd,
        task_prefix=task_prefix,
        report_days=report_days,
    )
    paths = _expected_paths(output_dir)

    scripts = {
        paths["run_auto"]: _runner_script(project_dir, python_cmd, "--auto", "auto.log"),
        paths["restart_auto"]: _restart_auto_script(project_dir, python_cmd, task_prefix),
        paths["run_doctor"]: _runner_script(project_dir, python_cmd, "--doctor", "doctor.log"),
        paths["run_report"]: _runner_script(project_dir, python_cmd, f"--ai-report --report-days {report_days}", "ai_report.log"),
        paths["run_status"]: _runner_script(project_dir, python_cmd, f"--ops-status --report-days {report_days}", "ops_status.log"),
        paths["install"]: _install_script(config, paths),
        paths["uninstall"]: _uninstall_script(task_prefix),
    }
    for path, content in scripts.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    return paths


def format_windows_task_summary(paths: Dict[str, str]) -> str:
    """格式化脚本生成结果"""
    lines = [
        "=" * 60,
        "Windows无人值守任务脚本已生成",
        "=" * 60,
    ]
    for key in ["run_auto", "restart_auto", "run_doctor", "run_report", "run_status", "install", "uninstall"]:
        lines.append(f"{key}: {paths.get(key)}")
    lines.extend([
        "-" * 60,
        "安装方式:",
        "  1. 用管理员 PowerShell 打开输出目录",
        "  2. 执行: Set-ExecutionPolicy -Scope Process Bypass",
        "  3. 执行: .\\install_tasks.ps1",
        "卸载方式:",
        "  执行: .\\uninstall_tasks.ps1",
        "=" * 60,
    ])
    return "\n".join(lines)


def _read_tail(path: str, max_bytes: int = 20000) -> str:
    """读取日志尾部"""
    if not os.path.exists(path):
        return ""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(-max_bytes, os.SEEK_END)
        data = f.read()
    return data.decode("utf-8", errors="ignore")


def _parse_log_tail(text: str) -> Dict:
    """解析运行脚本日志中的最近START/END和退出码"""
    starts = re.findall(r"===== (.*?) START (.*?) =====", text)
    ends = re.findall(r"===== (.*?) END exit=([-\d]+) =====", text)
    return {
        "last_start": starts[-1][0] if starts else "",
        "last_args": starts[-1][1] if starts else "",
        "last_end": ends[-1][0] if ends else "",
        "last_exit": int(ends[-1][1]) if ends else None,
    }


def _check_script(path: str, key: str) -> UnattendedItem:
    """检查生成脚本是否存在且保持模拟盘保护"""
    if not os.path.exists(path):
        return UnattendedItem(
            name=f"脚本:{key}",
            ok=False,
            severity="critical",
            detail=f"不存在: {path}",
            suggestion="运行 python main.py --windows-tasks 重新生成脚本。",
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if key.startswith("run_") and "$env:BROKER_MODE = 'paper'" not in content:
            return UnattendedItem(
                name=f"脚本:{key}",
                ok=False,
                severity="critical",
                detail="未发现 BROKER_MODE=paper 保护",
                suggestion="重新生成脚本，确保无人值守默认模拟盘。",
            )
        return UnattendedItem(f"脚本:{key}", True, "ok", path)
    except Exception as e:
        return UnattendedItem(f"脚本:{key}", False, "warn", str(e))


def _check_logs(project_dir: str) -> List[UnattendedItem]:
    """检查无人值守运行日志"""
    items = []
    log_dir = os.path.join(project_dir, "logs")
    for name, log_name in LOG_NAMES.items():
        path = os.path.join(log_dir, log_name)
        text = _read_tail(path)
        if not text:
            items.append(UnattendedItem(
                name=f"日志:{name}",
                ok=False,
                severity="warn",
                detail=f"暂无日志: {path}",
                suggestion="安装计划任务后等待下一次触发，或手动运行对应 run_*.ps1。",
            ))
            continue
        parsed = _parse_log_tail(text)
        last_exit = parsed.get("last_exit")
        if name == "Auto" and parsed.get("last_start") and not parsed.get("last_end"):
            items.append(UnattendedItem(
                name=f"日志:{name}",
                ok=True,
                severity="ok",
                detail=(
                    f"running_since={parsed.get('last_start')} "
                    f"args={parsed.get('last_args')} 尚无END，符合长驻--auto日志形态"
                ),
            ))
            continue
        if name == "Auto-Restart" and parsed.get("last_start") and not parsed.get("last_end"):
            items.append(UnattendedItem(
                name=f"日志:{name}",
                ok=False,
                severity="warn",
                detail=(
                    f"last_start={parsed.get('last_start')} "
                    f"args={parsed.get('last_args')} 尚无END"
                ),
                suggestion="重启脚本应快速退出，若长期无END请检查任务计划程序。",
            ))
            continue
        ok = last_exit == 0
        severity = "ok" if ok else "warn"
        detail = (
            f"last_start={parsed.get('last_start')} "
            f"last_end={parsed.get('last_end')} "
            f"exit={last_exit} args={parsed.get('last_args')}"
        )
        items.append(UnattendedItem(
            name=f"日志:{name}",
            ok=ok,
            severity=severity,
            detail=detail,
            suggestion="查看 logs 目录定位最近一次失败。" if not ok else "",
        ))
    return items


def run_unattended_status(
    project_dir: str = None,
    output_dir: str = None,
    task_prefix: str = DEFAULT_TASK_PREFIX,
    task_query_func: Callable[[str], Dict[str, Dict]] = None,
    check_scheduled_tasks: bool = True,
) -> List[UnattendedItem]:
    """
    检查Windows无人值守安装和运行痕迹

    该函数只读脚本、日志和计划任务状态，不注册/删除任务。
    """
    project_dir = os.path.abspath(project_dir or os.getcwd())
    output_dir = os.path.abspath(output_dir or DEFAULT_OUTPUT_DIR)
    paths = _expected_paths(output_dir)
    items: List[UnattendedItem] = []

    for key in SCRIPT_KEYS:
        items.append(_check_script(paths[key], key))

    if check_scheduled_tasks:
        try:
            query = task_query_func or _query_windows_tasks
            tasks = query(task_prefix)
            for name in TASK_NAMES:
                task_name = f"{task_prefix}-{name}"
                task = tasks.get(task_name)
                if not task:
                    items.append(UnattendedItem(
                        name=f"计划任务:{name}",
                        ok=False,
                        severity="warn",
                        detail=f"未安装: {task_name}",
                        suggestion="用管理员 PowerShell 执行 install_tasks.ps1。",
                    ))
                    continue
                result = task.get("LastTaskResult")
                ok = result in (None, 0, "0")
                items.append(UnattendedItem(
                    name=f"计划任务:{name}",
                    ok=ok,
                    severity="ok" if ok else "warn",
                    detail=(
                        f"state={task.get('State')} "
                        f"last_run={task.get('LastRunTime')} "
                        f"last_result={result}"
                    ),
                    suggestion="检查 Windows 任务计划程序中的最近运行结果。" if not ok else "",
                ))
        except Exception as e:
            items.append(UnattendedItem(
                name="计划任务读取",
                ok=False,
                severity="warn",
                detail=str(e),
                suggestion="如果不是Windows环境可忽略；否则检查 PowerShell Get-ScheduledTask 权限。",
            ))

    items.extend(_check_logs(project_dir))
    return items


def format_unattended_status(items: List[UnattendedItem]) -> str:
    """格式化无人值守巡检报告"""
    lines = [
        "=" * 60,
        "Windows无人值守状态",
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
        lines.append(f"结论: 无人值守未就绪，critical={critical} warn={warn}")
    elif warn:
        lines.append(f"结论: 无人值守可配置但仍有 warn={warn}")
    else:
        lines.append("结论: 无人值守脚本、任务和最近日志正常")
    lines.append("=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_windows_task_summary(generate_windows_task_scripts()))
