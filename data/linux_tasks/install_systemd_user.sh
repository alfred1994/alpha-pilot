#!/usr/bin/env bash
set -euo pipefail

SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

chmod +x '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/run_auto.sh' '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/restart_auto.sh' '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/run_doctor.sh' '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/run_report.sh' '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/run_status.sh' '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/run_closure_repair.sh'
install -m 0644 '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/systemd_user/alpha-pilot-auto.service' "$SYSTEMD_USER_DIR/alpha-pilot-auto.service"
install -m 0644 '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/systemd_user/alpha-pilot-auto-restart.service' "$SYSTEMD_USER_DIR/alpha-pilot-auto-restart.service"
install -m 0644 '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/systemd_user/alpha-pilot-auto-restart.timer' "$SYSTEMD_USER_DIR/alpha-pilot-auto-restart.timer"
install -m 0644 '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/systemd_user/alpha-pilot-doctor.service' "$SYSTEMD_USER_DIR/alpha-pilot-doctor.service"
install -m 0644 '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/systemd_user/alpha-pilot-doctor.timer' "$SYSTEMD_USER_DIR/alpha-pilot-doctor.timer"
install -m 0644 '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/systemd_user/alpha-pilot-report.service' "$SYSTEMD_USER_DIR/alpha-pilot-report.service"
install -m 0644 '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/systemd_user/alpha-pilot-report.timer' "$SYSTEMD_USER_DIR/alpha-pilot-report.timer"
install -m 0644 '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/systemd_user/alpha-pilot-status.service' "$SYSTEMD_USER_DIR/alpha-pilot-status.service"
install -m 0644 '/home/ubuntu/projects/alpha-pilot/data/linux_tasks/systemd_user/alpha-pilot-status.timer' "$SYSTEMD_USER_DIR/alpha-pilot-status.timer"

systemctl --user daemon-reload
systemctl --user enable --now alpha-pilot-auto.service
systemctl --user enable --now alpha-pilot-auto-restart.timer
systemctl --user enable --now alpha-pilot-doctor.timer
systemctl --user enable --now alpha-pilot-report.timer
systemctl --user enable --now alpha-pilot-status.timer

cat <<'EOF'
AlphaPilot systemd --user tasks installed.

建议在服务器上启用用户 lingering，保证Hermes用户退出后仍可运行：
  sudo loginctl enable-linger "$USER"

查看状态：
  systemctl --user status alpha-pilot-auto.service
  systemctl --user list-timers 'alpha-pilot-*'
  python3 main.py --linux-unattended-status
EOF
