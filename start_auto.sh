#!/bin/bash
# 启动自动盯盘
cd /home/ubuntu/projects/quant-pilot
source venv/bin/activate
source .env
export XIAOMI_API_KEY XIAOMI_BASE_URL TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID BROKER_MODE=paper

# 清理旧锁
rm -f data/auto_trader.lock

# 启动
python3 main.py --auto 2>&1
