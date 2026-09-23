#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR='/home/ubuntu/projects/alpha-pilot'
PYTHON_CMD='python3'
LOG_FILE='/home/ubuntu/projects/alpha-pilot/logs/db_maintenance.log'
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"

mkdir -p "$(dirname "$LOG_FILE")"
cd "$PROJECT_DIR"

# 日志轮转：单文件超过20MB时保留一个历史副本，避免 auto.log 无限增长。
if [ -f "$LOG_FILE" ]; then
  log_size=$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)
  if [ "$log_size" -gt 20971520 ]; then
    mv -f "$LOG_FILE" "${LOG_FILE}.1"
  fi
fi

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
echo "===== $stamp START --db-maintenance =====" >> "$LOG_FILE"
timeout --kill-after=15s 600s $PYTHON_CMD main.py --db-maintenance >> "$LOG_FILE" 2>&1
exit_code=$?
stamp="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== $stamp END exit=$exit_code =====" >> "$LOG_FILE"
exit $exit_code
