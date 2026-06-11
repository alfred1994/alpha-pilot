#!/bin/bash
# 事件驱动交易员启动脚本（Hermes部署用）

set -e

# 加载环境变量
if [ -f "$HOME/.hermes/.env" ]; then
    source "$HOME/.hermes/.env"
fi

# 项目目录
PROJECT_DIR="$HOME/projects/a-stock-quant"
cd "$PROJECT_DIR" || exit 1

# 日志目录
LOG_DIR="$HOME/.hermes/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/quant-realtime-$(date +%Y%m%d).log"

# 启动事件驱动交易员
echo "[$(date)] 事件驱动交易员启动" >> "$LOG_FILE"
python3 main.py --realtime >> "$LOG_FILE" 2>&1

# 退出记录
echo "[$(date)] 事件驱动交易员退出 code=$?" >> "$LOG_FILE"
