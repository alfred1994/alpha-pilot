"""
调度器模块
====================================================================
功能:
  1. 交易日历 - 判断交易日、获取下一交易日
  2. 全链路管道 - 选股 → 信号 → 决策 → 执行
  3. Telegram通知 - 推送交易信号和报告

使用方法:
    from scheduler.market_calendar import is_trading_day, next_trading_day
    from scheduler.pipeline import run_pipeline
    from scheduler.notifier import send_message, send_report
====================================================================
"""
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
logger = logging.getLogger("scheduler")
