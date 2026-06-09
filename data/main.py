"""
A股量化数据层 - 统一入口
使用方法:
    from data.realtime import get_realtime
    from data.history import get_daily
    from data.market import get_limit_up, get_dragon_tiger
    from data.sentiment import get_weibo_finance, get_stock_news
"""

# 快捷导入
from data.realtime import get_realtime, get_realtime_batch, RealtimeQuote
from data.history import get_daily, get_weekly, get_monthly, get_stock_list
from data.market import get_limit_up, get_limit_down, get_dragon_tiger, get_margin
from data.sentiment import get_weibo_finance, get_stock_news, get_market_sentiment
