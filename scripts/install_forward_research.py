"""生成独立研究 systemd 用户任务；显式 --install 才安装，不更改交易服务。"""
import argparse
from pathlib import Path
import subprocess


def units(python, runner, config, working_directory):
    def quote(value):
        value = str(value)
        if any(c in value for c in ("\n", "\r", "%")):
            raise ValueError("unsupported systemd path")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    quote(working_directory)  # 校验控制字符；WorkingDirectory 不使用 ExecStart 的引号语法。
    if not str(working_directory).startswith("/"):
        raise ValueError("working directory must be absolute")
    return {
        "alpha-pilot-forward-research.service": f"""[Unit]
Description=AlphaPilot frozen forward research (paper only)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory={working_directory}
Environment=BROKER_MODE=paper PYTHONUNBUFFERED=1
ExecStart={quote(python)} {quote(runner)} --config {quote(config)}
TimeoutStartSec=3000
CPUQuota=100%
MemoryMax=3G
Nice=10
NoNewPrivileges=true
""",
        "alpha-pilot-forward-research.timer": """[Unit]
Description=AlphaPilot forward research after close with bounded retries

[Timer]
OnCalendar=Mon..Fri *-*-* 16,17,20:20:00 Asia/Shanghai
OnCalendar=Mon..Fri *-*-* 08:30:00 Asia/Shanghai
Persistent=true
Unit=alpha-pilot-forward-research.service

[Install]
WantedBy=timers.target
""",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("python", "runner", "config", "working-directory", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, value in units(args.python, args.runner, args.config, args.working_directory).items():
        (args.output / name).write_text(value, encoding="utf-8")
    if args.install:
        if args.output.resolve() != (Path.home()/".config/systemd/user").resolve():
            parser.error("install output must be the user systemd directory")
        subprocess.run(["systemd-analyze", "--user", "verify",
                        str(args.output/"alpha-pilot-forward-research.service"),
                        str(args.output/"alpha-pilot-forward-research.timer")], check=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "alpha-pilot-forward-research.timer"], check=True)


if __name__ == "__main__":
    main()
