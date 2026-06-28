#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR='/home/ubuntu/projects/quant-pilot'
PYTHON_CMD='python3'
AUTO_UNIT='quant-pilot-auto.service'
LOG_FILE='/home/ubuntu/projects/quant-pilot/logs/auto_restart.log'
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"

mkdir -p "$(dirname "$LOG_FILE")"
cd "$PROJECT_DIR"

# 激活虚拟环境（兼容历史venv和README推荐.venv）
if [ -f "venv/bin/activate" ]; then
  source venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
fi

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
printf '%s\n' "$watchdog_output" >> "$LOG_FILE"

if [ "$watchdog_exit" -eq 0 ]; then
  echo "Watchdog OK, no restart needed." >> "$LOG_FILE"
  stamp="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "===== $stamp END exit=0 =====" >> "$LOG_FILE"
  exit 0
fi

if ! printf '%s\n' "$watchdog_output" | grep -Eq '自动盘锁|自动循环新鲜度|自动盯盘状态'; then
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
