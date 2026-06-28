#!/bin/bash
# 启动自动盯盘
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/projects/quant-pilot}"
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"
cd "$PROJECT_DIR"

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

# 清理残留锁文件（pid不存在则删除）
LOCK_FILE="$PROJECT_DIR/data/auto_trader.lock"
if [ -f "$LOCK_FILE" ]; then
  lock_pid=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('pid',''))" "$LOCK_FILE" 2>/dev/null || true)
  if [ -n "$lock_pid" ] && ! kill -0 "$lock_pid" 2>/dev/null; then
    rm -f "$LOCK_FILE"
  fi
fi

# 启动
python3 main.py --auto 2>&1
