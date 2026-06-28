#!/bin/bash
# A股量化系统 - 定时任务包装脚本
# 用法: ./run_quant.sh [scan|execute|review|full]
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/projects/quant-pilot}"
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"
cd "$PROJECT_DIR"

if [ -f "venv/bin/activate" ]; then
  source venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
fi

# 加载环境变量
if [ -f "$HERMES_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$HERMES_ENV_FILE"
  set +a
fi

export BROKER_MODE="${BROKER_MODE:-paper}"
export PYTHONUNBUFFERED=1

# 运行指定命令
CMD=${1:-full}
echo "=== $(date '+%Y-%m-%d %H:%M:%S') 执行 $CMD ==="
python3 main.py --$CMD 2>&1
echo "=== 完成 ==="
