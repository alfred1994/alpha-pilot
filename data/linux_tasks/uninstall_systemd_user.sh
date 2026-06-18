#!/usr/bin/env bash
set -euo pipefail

SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
systemctl --user disable --now quant-pilot-auto.service quant-pilot-auto-restart.service quant-pilot-auto-restart.timer quant-pilot-doctor.service quant-pilot-doctor.timer quant-pilot-report.service quant-pilot-report.timer quant-pilot-status.service quant-pilot-status.timer 2>/dev/null || true
for unit in quant-pilot-auto.service quant-pilot-auto-restart.service quant-pilot-auto-restart.timer quant-pilot-doctor.service quant-pilot-doctor.timer quant-pilot-report.service quant-pilot-report.timer quant-pilot-status.service quant-pilot-status.timer; do
  rm -f "$SYSTEMD_USER_DIR/$unit"
done
systemctl --user daemon-reload
echo "Quant Pilot systemd --user tasks removed."
