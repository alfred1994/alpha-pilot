#!/usr/bin/env bash
set -euo pipefail

SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

chmod +x '/home/ubuntu/projects/quant-pilot/data/linux_tasks/run_auto.sh' '/home/ubuntu/projects/quant-pilot/data/linux_tasks/restart_auto.sh' '/home/ubuntu/projects/quant-pilot/data/linux_tasks/run_doctor.sh' '/home/ubuntu/projects/quant-pilot/data/linux_tasks/run_report.sh' '/home/ubuntu/projects/quant-pilot/data/linux_tasks/run_status.sh' '/home/ubuntu/projects/quant-pilot/data/linux_tasks/run_closure_repair.sh'
install -m 0644 '/home/ubuntu/projects/quant-pilot/data/linux_tasks/systemd_user/quant-pilot-auto.service' "$SYSTEMD_USER_DIR/quant-pilot-auto.service"
install -m 0644 '/home/ubuntu/projects/quant-pilot/data/linux_tasks/systemd_user/quant-pilot-auto-restart.service' "$SYSTEMD_USER_DIR/quant-pilot-auto-restart.service"
install -m 0644 '/home/ubuntu/projects/quant-pilot/data/linux_tasks/systemd_user/quant-pilot-auto-restart.timer' "$SYSTEMD_USER_DIR/quant-pilot-auto-restart.timer"
install -m 0644 '/home/ubuntu/projects/quant-pilot/data/linux_tasks/systemd_user/quant-pilot-doctor.service' "$SYSTEMD_USER_DIR/quant-pilot-doctor.service"
install -m 0644 '/home/ubuntu/projects/quant-pilot/data/linux_tasks/systemd_user/quant-pilot-doctor.timer' "$SYSTEMD_USER_DIR/quant-pilot-doctor.timer"
install -m 0644 '/home/ubuntu/projects/quant-pilot/data/linux_tasks/systemd_user/quant-pilot-report.service' "$SYSTEMD_USER_DIR/quant-pilot-report.service"
install -m 0644 '/home/ubuntu/projects/quant-pilot/data/linux_tasks/systemd_user/quant-pilot-report.timer' "$SYSTEMD_USER_DIR/quant-pilot-report.timer"
install -m 0644 '/home/ubuntu/projects/quant-pilot/data/linux_tasks/systemd_user/quant-pilot-status.service' "$SYSTEMD_USER_DIR/quant-pilot-status.service"
install -m 0644 '/home/ubuntu/projects/quant-pilot/data/linux_tasks/systemd_user/quant-pilot-status.timer' "$SYSTEMD_USER_DIR/quant-pilot-status.timer"

systemctl --user daemon-reload
systemctl --user enable --now quant-pilot-auto.service
systemctl --user enable --now quant-pilot-auto-restart.timer
systemctl --user enable --now quant-pilot-doctor.timer
systemctl --user enable --now quant-pilot-report.timer
systemctl --user enable --now quant-pilot-status.timer

cat <<'EOF'
Quant Pilot systemd --user tasks installed.

建议在服务器上启用用户 lingering，保证Hermes用户退出后仍可运行：
  sudo loginctl enable-linger "$USER"

查看状态：
  systemctl --user status quant-pilot-auto.service
  systemctl --user list-timers 'quant-pilot-*'
  python3 main.py --linux-unattended-status
EOF
