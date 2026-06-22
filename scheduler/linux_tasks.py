"""
Linux/Hermes无人值守任务脚本生成器
====================================================================
为Ubuntu上的Hermes Agent生成一组 systemd --user 脚本和单元：
  - run_auto.sh: 启动自动盯盘长驻循环
  - restart_auto.sh: Watchdog发现运行态故障时重启Auto service
  - run_doctor.sh: 执行Watchdog巡检、自愈和闭环修复
  - run_report.sh: 生成最近N天试运行报告
  - run_status.sh: 生成一页式运维状态
  - install_systemd_user.sh: 安装并启用用户级systemd服务/定时器
  - uninstall_systemd_user.sh: 停用并删除用户级systemd服务/定时器

脚本默认 source ~/.hermes/.env，并强制 BROKER_MODE=paper。
====================================================================
"""
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List

from config import DATA_DIR
from scheduler.windows_tasks import UnattendedItem


DEFAULT_OUTPUT_DIR = os.path.join(DATA_DIR, "linux_tasks")
DEFAULT_SERVICE_PREFIX = "quant-pilot"
SCRIPT_KEYS = [
    "run_auto",
    "restart_auto",
    "run_doctor",
    "run_report",
    "run_status",
    "run_closure_repair",
    "install",
    "uninstall",
]
SERVICE_KEYS = [
    "auto_service",
    "auto_restart_service",
    "auto_restart_timer",
    "doctor_service",
    "doctor_timer",
    "report_service",
    "report_timer",
    "status_service",
    "status_timer",
]
LOG_NAMES = {
    "Auto": "auto.log",
    "Auto-Restart": "auto_restart.log",
    "Doctor": "doctor.log",
    "Report": "ai_report.log",
    "Status": "ops_status.log",
}


@dataclass
class LinuxTaskConfig:
    """Linux systemd用户级任务配置"""
    project_dir: str
    output_dir: str = DEFAULT_OUTPUT_DIR
    python_cmd: str = "python3"
    service_prefix: str = DEFAULT_SERVICE_PREFIX
    report_days: int = 5
    hermes_env_file: str = "$HOME/.hermes/.env"
    report_time: str = "15:40"
    status_time: str = "15:50"


def _sh_quote(value: str) -> str:
    """Shell单引号转义"""
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _unit_name(prefix: str, name: str, suffix: str = "service") -> str:
    """生成systemd unit名称"""
    safe_prefix = re.sub(r"[^A-Za-z0-9_.@-]+", "-", prefix.strip() or DEFAULT_SERVICE_PREFIX)
    return f"{safe_prefix}-{name}.{suffix}"


def _runner_script(project_dir: str, python_cmd: str, args: str,
                   log_name: str, hermes_env_file: str = "$HOME/.hermes/.env",
                   timeout_seconds: int = 0,
                   tolerate_failure: bool = False) -> str:
    """生成Linux运行脚本"""
    log_path = os.path.join(project_dir, "logs", log_name)
    if timeout_seconds:
        command = (
            f"timeout --kill-after=15s {int(timeout_seconds)}s "
            f"$PYTHON_CMD main.py {args} >> \"$LOG_FILE\" 2>&1"
        )
    else:
        command = f"$PYTHON_CMD main.py {args} >> \"$LOG_FILE\" 2>&1"
    final_exit = "0" if tolerate_failure else "$exit_code"
    return f"""#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR={_sh_quote(project_dir)}
PYTHON_CMD={_sh_quote(python_cmd)}
LOG_FILE={_sh_quote(log_path)}
HERMES_ENV_FILE="${{HERMES_ENV_FILE:-{hermes_env_file}}}"

mkdir -p "$(dirname "$LOG_FILE")"
cd "$PROJECT_DIR"

if [ -f "$HERMES_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$HERMES_ENV_FILE"
  set +a
fi

export BROKER_MODE=paper
export PYTHONUNBUFFERED=1

stamp="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== $stamp START {args} =====" >> "$LOG_FILE"
{command}
exit_code=$?
stamp="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== $stamp END exit=$exit_code =====" >> "$LOG_FILE"
exit {final_exit}
"""


def _restart_auto_script(project_dir: str, python_cmd: str, service_prefix: str,
                         hermes_env_file: str = "$HOME/.hermes/.env") -> str:
    """生成Linux自动盘重启脚本"""
    log_path = os.path.join(project_dir, "logs", "auto_restart.log")
    auto_unit = _unit_name(service_prefix, "auto")
    return f"""#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR={_sh_quote(project_dir)}
PYTHON_CMD={_sh_quote(python_cmd)}
AUTO_UNIT={_sh_quote(auto_unit)}
LOG_FILE={_sh_quote(log_path)}
HERMES_ENV_FILE="${{HERMES_ENV_FILE:-{hermes_env_file}}}"

mkdir -p "$(dirname "$LOG_FILE")"
cd "$PROJECT_DIR"

if [ -f "$HERMES_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$HERMES_ENV_FILE"
  set +a
fi

export BROKER_MODE=paper
export PYTHONUNBUFFERED=1

stamp="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== $stamp START restart-auto =====" >> "$LOG_FILE"

watchdog_output="$(timeout --kill-after=15s 180s $PYTHON_CMD main.py --watchdog 2>&1)"
watchdog_exit=$?
printf '%s\\n' "$watchdog_output" >> "$LOG_FILE"

if [ "$watchdog_exit" -eq 0 ]; then
  echo "Watchdog OK, no restart needed." >> "$LOG_FILE"
  stamp="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "===== $stamp END exit=0 =====" >> "$LOG_FILE"
  exit 0
fi

if ! printf '%s\\n' "$watchdog_output" | grep -Eq '自动盘锁|自动循环新鲜度|自动盯盘状态'; then
  echo "Watchdog failed but no auto-runtime fault, skip restart." >> "$LOG_FILE"
  stamp="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "===== $stamp END exit=$watchdog_exit =====" >> "$LOG_FILE"
  exit "$watchdog_exit"
fi

echo "Watchdog exit=$watchdog_exit, restarting $AUTO_UNIT" >> "$LOG_FILE"
systemctl --user restart "$AUTO_UNIT" >> "$LOG_FILE" 2>&1
restart_exit=$?
stamp="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== $stamp END exit=$restart_exit =====" >> "$LOG_FILE"
exit "$restart_exit"
"""


def _service_unit(description: str, script_path: str,
                  restart: bool = False) -> str:
    """生成systemd service unit"""
    restart_block = "\nRestart=always\nRestartSec=10" if restart else ""
    return f"""[Unit]
Description={description}
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/env bash {script_path}
Environment=BROKER_MODE=paper
Environment=PYTHONUNBUFFERED=1{restart_block}

[Install]
WantedBy=default.target
"""


def _oneshot_service_unit(description: str, script_path: str,
                          timeout_start_sec: int = 0) -> str:
    """生成oneshot systemd service unit"""
    timeout_block = f"TimeoutStartSec={int(timeout_start_sec)}\n" if timeout_start_sec else ""
    return f"""[Unit]
Description={description}
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env bash {script_path}
Environment=BROKER_MODE=paper
Environment=PYTHONUNBUFFERED=1
{timeout_block}"""


def _interval_timer_unit(description: str, service_unit: str,
                         boot_sec: str, active_sec: str) -> str:
    """生成间隔触发timer"""
    return f"""[Unit]
Description={description}

[Timer]
OnBootSec={boot_sec}
OnUnitActiveSec={active_sec}
Unit={service_unit}
Persistent=true

[Install]
WantedBy=timers.target
"""


def _calendar_timer_unit(description: str, service_unit: str,
                         calendar: str) -> str:
    """生成日历触发timer"""
    return f"""[Unit]
Description={description}

[Timer]
OnCalendar={calendar}
Unit={service_unit}
Persistent=true

[Install]
WantedBy=timers.target
"""


def _expected_paths(output_dir: str, service_prefix: str = DEFAULT_SERVICE_PREFIX) -> Dict[str, str]:
    """根据输出目录生成路径字典"""
    units_dir = os.path.join(output_dir, "systemd_user")
    paths = {
        "run_auto": os.path.join(output_dir, "run_auto.sh"),
        "restart_auto": os.path.join(output_dir, "restart_auto.sh"),
        "run_doctor": os.path.join(output_dir, "run_doctor.sh"),
        "run_report": os.path.join(output_dir, "run_report.sh"),
        "run_status": os.path.join(output_dir, "run_status.sh"),
        "run_closure_repair": os.path.join(output_dir, "run_closure_repair.sh"),
        "install": os.path.join(output_dir, "install_systemd_user.sh"),
        "uninstall": os.path.join(output_dir, "uninstall_systemd_user.sh"),
        "units_dir": units_dir,
    }
    paths.update({
        "auto_service": os.path.join(units_dir, _unit_name(service_prefix, "auto")),
        "auto_restart_service": os.path.join(units_dir, _unit_name(service_prefix, "auto-restart")),
        "auto_restart_timer": os.path.join(units_dir, _unit_name(service_prefix, "auto-restart", "timer")),
        "doctor_service": os.path.join(units_dir, _unit_name(service_prefix, "doctor")),
        "doctor_timer": os.path.join(units_dir, _unit_name(service_prefix, "doctor", "timer")),
        "report_service": os.path.join(units_dir, _unit_name(service_prefix, "report")),
        "report_timer": os.path.join(units_dir, _unit_name(service_prefix, "report", "timer")),
        "status_service": os.path.join(units_dir, _unit_name(service_prefix, "status")),
        "status_timer": os.path.join(units_dir, _unit_name(service_prefix, "status", "timer")),
    })
    return paths


def _install_script(config: LinuxTaskConfig, paths: Dict[str, str]) -> str:
    """生成systemd用户级安装脚本"""
    units = [
        os.path.basename(paths["auto_service"]),
        os.path.basename(paths["auto_restart_service"]),
        os.path.basename(paths["auto_restart_timer"]),
        os.path.basename(paths["doctor_service"]),
        os.path.basename(paths["doctor_timer"]),
        os.path.basename(paths["report_service"]),
        os.path.basename(paths["report_timer"]),
        os.path.basename(paths["status_service"]),
        os.path.basename(paths["status_timer"]),
    ]
    unit_lines = "\n".join(f"install -m 0644 { _sh_quote(os.path.join(paths['units_dir'], unit)) } \"$SYSTEMD_USER_DIR/{unit}\"" for unit in units)
    return f"""#!/usr/bin/env bash
set -euo pipefail

SYSTEMD_USER_DIR="${{XDG_CONFIG_HOME:-$HOME/.config}}/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

chmod +x {_sh_quote(paths['run_auto'])} {_sh_quote(paths['restart_auto'])} {_sh_quote(paths['run_doctor'])} {_sh_quote(paths['run_report'])} {_sh_quote(paths['run_status'])} {_sh_quote(paths['run_closure_repair'])}
{unit_lines}

systemctl --user daemon-reload
systemctl --user enable --now {os.path.basename(paths['auto_service'])}
systemctl --user enable --now {os.path.basename(paths['auto_restart_timer'])}
systemctl --user enable --now {os.path.basename(paths['doctor_timer'])}
systemctl --user enable --now {os.path.basename(paths['report_timer'])}
systemctl --user enable --now {os.path.basename(paths['status_timer'])}

cat <<'EOF'
Quant Pilot systemd --user tasks installed.

建议在服务器上启用用户 lingering，保证Hermes用户退出后仍可运行：
  sudo loginctl enable-linger "$USER"

查看状态：
  systemctl --user status {os.path.basename(paths['auto_service'])}
  systemctl --user list-timers '{config.service_prefix}-*'
  python3 main.py --linux-unattended-status
EOF
"""


def _uninstall_script(service_prefix: str) -> str:
    """生成systemd用户级卸载脚本"""
    units = [
        _unit_name(service_prefix, "auto"),
        _unit_name(service_prefix, "auto-restart"),
        _unit_name(service_prefix, "auto-restart", "timer"),
        _unit_name(service_prefix, "doctor"),
        _unit_name(service_prefix, "doctor", "timer"),
        _unit_name(service_prefix, "report"),
        _unit_name(service_prefix, "report", "timer"),
        _unit_name(service_prefix, "status"),
        _unit_name(service_prefix, "status", "timer"),
    ]
    units_text = " ".join(units)
    return f"""#!/usr/bin/env bash
set -euo pipefail

SYSTEMD_USER_DIR="${{XDG_CONFIG_HOME:-$HOME/.config}}/systemd/user"
systemctl --user disable --now {units_text} 2>/dev/null || true
for unit in {units_text}; do
  rm -f "$SYSTEMD_USER_DIR/$unit"
done
systemctl --user daemon-reload
echo "Quant Pilot systemd --user tasks removed."
"""


def _chmod_executable(path: str):
    """添加可执行权限"""
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def generate_linux_task_scripts(
    project_dir: str = None,
    output_dir: str = None,
    python_cmd: str = "python3",
    service_prefix: str = DEFAULT_SERVICE_PREFIX,
    report_days: int = 5,
    hermes_env_file: str = "$HOME/.hermes/.env",
) -> Dict[str, str]:
    """
    生成Ubuntu/Hermes无人值守systemd用户级脚本

    Returns:
        路径字典
    """
    project_dir = os.path.abspath(project_dir or os.getcwd())
    output_dir = os.path.abspath(output_dir or DEFAULT_OUTPUT_DIR)
    os.makedirs(output_dir, exist_ok=True)
    paths = _expected_paths(output_dir, service_prefix)
    os.makedirs(paths["units_dir"], exist_ok=True)
    config = LinuxTaskConfig(
        project_dir=project_dir,
        output_dir=output_dir,
        python_cmd=python_cmd,
        service_prefix=service_prefix,
        report_days=report_days,
        hermes_env_file=hermes_env_file,
    )

    scripts = {
        paths["run_auto"]: _runner_script(project_dir, python_cmd, "--auto", "auto.log", hermes_env_file),
        paths["restart_auto"]: _restart_auto_script(project_dir, python_cmd, service_prefix, hermes_env_file),
        paths["run_doctor"]: _runner_script(project_dir, python_cmd, "--doctor", "doctor.log", hermes_env_file, timeout_seconds=240),
        paths["run_report"]: _runner_script(project_dir, python_cmd, f"--ai-report --report-days {report_days}", "ai_report.log", hermes_env_file),
        paths["run_status"]: _runner_script(project_dir, python_cmd, f"--ops-status --report-days {report_days}", "ops_status.log", hermes_env_file, tolerate_failure=True),
        paths["run_closure_repair"]: _runner_script(project_dir, python_cmd, "--closure-repair", "closure_repair.log", hermes_env_file),
        paths["install"]: _install_script(config, paths),
        paths["uninstall"]: _uninstall_script(service_prefix),
    }
    units = {
        paths["auto_service"]: _service_unit("Quant Pilot 模拟盘自动盯盘长驻循环", paths["run_auto"], restart=True),
        paths["auto_restart_service"]: _oneshot_service_unit("Quant Pilot 自动盘Watchdog重启", paths["restart_auto"], timeout_start_sec=240),
        paths["auto_restart_timer"]: _interval_timer_unit(
            "Quant Pilot 自动盘每10分钟Watchdog重启保护",
            os.path.basename(paths["auto_restart_service"]),
            "5min",
            "10min",
        ),
        paths["doctor_service"]: _oneshot_service_unit("Quant Pilot Watchdog巡检/自愈/闭环修复", paths["run_doctor"], timeout_start_sec=300),
        paths["doctor_timer"]: _interval_timer_unit("Quant Pilot Doctor每5分钟巡检", os.path.basename(paths["doctor_service"]), "2min", "5min"),
        paths["report_service"]: _oneshot_service_unit("Quant Pilot AI交易员模拟盘报告", paths["run_report"]),
        paths["report_timer"]: _calendar_timer_unit("Quant Pilot 盘后AI报告", os.path.basename(paths["report_service"]), f"Mon..Fri *-*-* {config.report_time}:00"),
        paths["status_service"]: _oneshot_service_unit("Quant Pilot 自动盯盘运维状态", paths["run_status"]),
        paths["status_timer"]: _calendar_timer_unit("Quant Pilot 盘后运维状态", os.path.basename(paths["status_service"]), f"Mon..Fri *-*-* {config.status_time}:00"),
    }
    for path, content in {**scripts, **units}.items():
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    for key in SCRIPT_KEYS:
        _chmod_executable(paths[key])
    return paths


def format_linux_task_summary(paths: Dict[str, str]) -> str:
    """格式化Linux脚本生成结果"""
    lines = [
        "=" * 60,
        "Ubuntu/Hermes无人值守systemd用户任务已生成",
        "=" * 60,
    ]
    for key in SCRIPT_KEYS + SERVICE_KEYS:
        lines.append(f"{key}: {paths.get(key)}")
    lines.extend([
        "-" * 60,
        "安装方式:",
        "  1. 确认 ~/.hermes/.env 已配置 LONGPORT_* 等环境变量",
        "  2. 执行: bash data/linux_tasks/install_systemd_user.sh",
        "  3. 建议执行: sudo loginctl enable-linger \"$USER\"",
        "  4. 复查: python3 main.py --linux-unattended-status",
        "卸载方式:",
        "  执行: bash data/linux_tasks/uninstall_systemd_user.sh",
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
    """解析运行日志中的最近START/END和退出码"""
    starts = re.findall(r"===== (.*?) START (.*?) =====", text)
    ends = re.findall(r"===== (.*?) END exit=([-\d]+) =====", text)
    return {
        "last_start": starts[-1][0] if starts else "",
        "last_args": starts[-1][1] if starts else "",
        "last_end": ends[-1][0] if ends else "",
        "last_exit": int(ends[-1][1]) if ends else None,
    }


def _query_systemd_units(service_prefix: str) -> Dict[str, Dict]:
    """读取systemd --user unit状态"""
    units = [
        _unit_name(service_prefix, "auto"),
        _unit_name(service_prefix, "auto-restart", "timer"),
        _unit_name(service_prefix, "doctor", "timer"),
        _unit_name(service_prefix, "report", "timer"),
        _unit_name(service_prefix, "status", "timer"),
    ]
    result = {}
    for unit in units:
        proc = subprocess.run(
            [
                "systemctl", "--user", "show", unit,
                "--property=LoadState,ActiveState,SubState,UnitFileState,Result",
                "--no-page",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        data = {"returncode": proc.returncode}
        for line in (proc.stdout or "").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                data[key] = value
        if proc.stderr:
            data["stderr"] = proc.stderr.strip()
        result[unit] = data
    return result


def _check_script(path: str, key: str) -> UnattendedItem:
    """检查脚本存在、可执行且有Hermes/env/paper保护"""
    if not os.path.exists(path):
        return UnattendedItem(f"脚本:{key}", False, "critical", f"不存在: {path}", "运行 python3 main.py --linux-tasks 重新生成。")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        executable = os.access(path, os.X_OK)
        if not executable:
            return UnattendedItem(f"脚本:{key}", False, "critical", "脚本不可执行", f"chmod +x {path}")
        if key not in ("install", "uninstall") and "export BROKER_MODE=paper" not in content:
            return UnattendedItem(f"脚本:{key}", False, "critical", "未发现 BROKER_MODE=paper 保护", "重新生成脚本。")
        if key not in ("install", "uninstall") and ".hermes/.env" not in content:
            return UnattendedItem(f"脚本:{key}", False, "warn", "未发现 ~/.hermes/.env 加载逻辑", "重新生成脚本。")
        return UnattendedItem(f"脚本:{key}", True, "ok", path)
    except Exception as e:
        return UnattendedItem(f"脚本:{key}", False, "warn", str(e))


def _check_logs(project_dir: str) -> List[UnattendedItem]:
    """检查Linux无人值守日志"""
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
                suggestion="安装systemd用户任务后等待触发，或手动运行对应 run_*.sh。",
            ))
            continue
        parsed = _parse_log_tail(text)
        if name == "Auto" and parsed.get("last_start") and not parsed.get("last_end"):
            items.append(UnattendedItem(
                name=f"日志:{name}",
                ok=True,
                severity="ok",
                detail=f"running_since={parsed.get('last_start')} args={parsed.get('last_args')} 尚无END，符合长驻--auto日志形态",
            ))
            continue
        last_exit = parsed.get("last_exit")
        ok = last_exit == 0
        items.append(UnattendedItem(
            name=f"日志:{name}",
            ok=ok,
            severity="ok" if ok else "warn",
            detail=(
                f"last_start={parsed.get('last_start')} "
                f"last_end={parsed.get('last_end')} "
                f"exit={last_exit} args={parsed.get('last_args')}"
            ),
            suggestion="查看 logs 目录定位最近一次失败。" if not ok else "",
        ))
    return items


def run_linux_unattended_status(
    project_dir: str = None,
    output_dir: str = None,
    service_prefix: str = DEFAULT_SERVICE_PREFIX,
    systemd_query_func: Callable[[str], Dict[str, Dict]] = None,
    check_systemd: bool = True,
) -> List[UnattendedItem]:
    """检查Linux/Hermes无人值守安装和运行痕迹"""
    project_dir = os.path.abspath(project_dir or os.getcwd())
    output_dir = os.path.abspath(output_dir or DEFAULT_OUTPUT_DIR)
    paths = _expected_paths(output_dir, service_prefix)
    items: List[UnattendedItem] = []

    for key in SCRIPT_KEYS:
        items.append(_check_script(paths[key], key))
    for key in SERVICE_KEYS:
        if not os.path.exists(paths[key]):
            items.append(UnattendedItem(f"systemd:{key}", False, "critical", f"不存在: {paths[key]}", "运行 python3 main.py --linux-tasks 重新生成。"))
        else:
            items.append(UnattendedItem(f"systemd:{key}", True, "ok", paths[key]))

    if check_systemd:
        try:
            query = systemd_query_func or _query_systemd_units
            units = query(service_prefix)
            for unit, data in units.items():
                load = data.get("LoadState", "")
                active = data.get("ActiveState", "")
                enabled = data.get("UnitFileState", "")
                ok = load == "loaded" and active in ("active", "activating") and enabled in ("enabled", "linked", "static")
                items.append(UnattendedItem(
                    name=f"systemd状态:{unit}",
                    ok=ok,
                    severity="ok" if ok else "warn",
                    detail=f"load={load} active={active} sub={data.get('SubState', '')} enabled={enabled} result={data.get('Result', '')}",
                    suggestion="执行 bash data/linux_tasks/install_systemd_user.sh，并确认 systemctl --user 可用。" if not ok else "",
                ))
        except Exception as e:
            items.append(UnattendedItem(
                "systemd读取",
                False,
                "warn",
                str(e),
                "在Ubuntu/Hermes用户下执行 systemctl --user status；必要时启用 loginctl linger。",
            ))

    items.extend(_check_logs(project_dir))
    return items


def format_linux_unattended_status(items: List[UnattendedItem]) -> str:
    """格式化Linux无人值守巡检报告"""
    lines = [
        "=" * 60,
        "Ubuntu/Hermes无人值守状态",
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
        lines.append(f"结论: Linux/Hermes无人值守未就绪，critical={critical} warn={warn}")
    elif warn:
        lines.append(f"结论: Linux/Hermes无人值守可配置但仍有 warn={warn}")
    else:
        lines.append("结论: Linux/Hermes无人值守脚本、systemd用户任务和最近日志正常")
    lines.append("=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_linux_task_summary(generate_linux_task_scripts()))
