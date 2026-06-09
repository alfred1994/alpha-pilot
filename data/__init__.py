"""
A股量化数据层 - 统一数据接口
数据源: 腾讯API(实时) + Baostock(历史) + AKShare(资金/涨停/龙虎榜/新闻)
"""
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
logger = logging.getLogger("data")

# 数据源调度器 - 自动限流
_last_call_time = {}

def rate_limit(source: str, min_interval: float = 2.0):
    """AKShare等有隐式限流的数据源，强制间隔"""
    now = time.time()
    last = _last_call_time.get(source, 0)
    wait = min_interval - (now - last)
    if wait > 0:
        time.sleep(wait)
    _last_call_time[source] = time.time()
