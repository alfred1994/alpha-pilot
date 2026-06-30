#!/bin/bash
# 事件驱动交易员启动脚本（Hermes部署用）

set -e

# 加载环境变量
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"
if [ -f "$HERMES_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$HERMES_ENV_FILE"
  set +a
fi

# 项目目录
PROJECT_DIR="${PROJECT_DIR:-$HOME/projects/alpha-pilot}"
cd "$PROJECT_DIR" || exit 1

if [ -f "venv/bin/activate" ]; then
  source venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
fi

export BROKER_MODE="${BROKER_MODE:-paper}"
export PYTHONUNBUFFERED=1

# 日志目录
LOG_DIR="$HOME/.hermes/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/quant-realtime-$(date +%Y%m%d).log"

# 启动事件驱动交易员
echo "[$(date)] 事件驱动交易员启动" >> "$LOG_FILE"
python3 main.py --realtime >> "$LOG_FILE" 2>&1

# 退出记录
echo "[$(date)] 事件驱动交易员退出 code=$?" >> "$LOG_FILE"
