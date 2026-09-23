#!/usr/bin/env bash
set -euo pipefail

SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
systemctl --user disable --now alpha-pilot-auto.service alpha-pilot-auto-restart.service alpha-pilot-auto-restart.timer alpha-pilot-doctor.service alpha-pilot-doctor.timer alpha-pilot-report.service alpha-pilot-report.timer alpha-pilot-status.service alpha-pilot-status.timer alpha-pilot-research.service alpha-pilot-research.timer alpha-pilot-pooled-ml.service alpha-pilot-pooled-ml.timer alpha-pilot-db-maintenance.service alpha-pilot-db-maintenance.timer alpha-pilot-laya-text.service alpha-pilot-laya-text.timer 2>/dev/null || true
for unit in alpha-pilot-auto.service alpha-pilot-auto-restart.service alpha-pilot-auto-restart.timer alpha-pilot-doctor.service alpha-pilot-doctor.timer alpha-pilot-report.service alpha-pilot-report.timer alpha-pilot-status.service alpha-pilot-status.timer alpha-pilot-research.service alpha-pilot-research.timer alpha-pilot-pooled-ml.service alpha-pilot-pooled-ml.timer alpha-pilot-db-maintenance.service alpha-pilot-db-maintenance.timer alpha-pilot-laya-text.service alpha-pilot-laya-text.timer; do
  rm -f "$SYSTEMD_USER_DIR/$unit"
done
systemctl --user daemon-reload
echo "AlphaPilot systemd --user tasks removed."
