#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR='/home/ubuntu/projects/quant-pilot'
PYTHON_CMD='python3'
LOG_FILE='/home/ubuntu/projects/quant-pilot/logs/auto.log'
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"

mkdir -p "$(dirname "$LOG_FILE")"
cd "$PROJECT_DIR"

# 激活虚拟环境
if [ -f "venv/bin/activate" ]; then
  source venv/bin/activate
fi

if [ -f "$HERMES_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$HERMES_ENV_FILE"
  set +a
fi

export BROKER_MODE=paper
export PYTHONUNBUFFERED=1

# 清理残留锁文件（pid 不存在则删除）
LOCK_FILE="$PROJECT_DIR/data/auto_trader.lock"
if [ -f "$LOCK_FILE" ]; then
  lock_pid=$(grep -oP 'pid=\K\d+' "$LOCK_FILE" 2>/dev/null || true)
  if [ -n "$lock_pid" ] && ! kill -0 "$lock_pid" 2>/dev/null; then
    echo "清理残留锁文件 (pid=$lock_pid 已不存在)" >> "$LOG_FILE"
    rm -f "$LOCK_FILE"
  fi
fi

stamp="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== $stamp START --auto =====" >> "$LOG_FILE"
$PYTHON_CMD main.py --auto >> "$LOG_FILE" 2>&1
exit_code=$?
stamp="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== $stamp END exit=$exit_code =====" >> "$LOG_FILE"
exit $exit_code
