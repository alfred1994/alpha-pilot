"""
交易日历模块
====================================================================
功能:
  1. 判断某天是否为A股交易日
  2. 获取下一交易日/上一交易日
  3. 获取交易时段(开盘/收盘时间)
  4. 获取当前市场状态(盘前/盘中/盘后/休市)

数据源: Baostock 交易日历，本地缓存
====================================================================
"""
from datetime import datetime, timedelta, time as dt_time, timezone
from typing import Optional, List
from scheduler import logger

# 北京时区 (UTC+8)
_BJ_TZ = timezone(timedelta(hours=8))


def _now_bj() -> datetime:
    """获取当前北京时间（A股使用 UTC+8）"""
    return datetime.now(_BJ_TZ).replace(tzinfo=None)

# 本地缓存
_trading_dates_cache: set = None
_cache_date: str = None

# A股交易所节假日休市表（仅列工作日休市；周末由工作日兜底层自然排除）。
# 仅作降级用，权威口径仍是 Baostock 交易日历；表外年份自动退回
# "周一至五全是交易日"。节假安排逐年公布，新年份需人工补表。
# 来源：2024/2025 为交易所公告；2026 为国务院办公节日安排（逢周末顺延后
# 的实际工作日休市）。
_HOLIDAY_WEEKDAYS: dict = {
    2024: {
        "0101",                                    # 元旦
        "0209", "0212", "0213", "0214", "0215", "0216",  # 春节(2/9-2/17)
        "0404", "0405",                            # 清明(4/4-4/6)
        "0501", "0502", "0503",                    # 劳动节(5/1-5/5)
        "0610",                                    # 端午
        "0916", "0917",                            # 中秋(9/15-9/17)
        "1001", "1002", "1003", "1004", "1007",    # 国庆(10/1-10/7)
    },
    2025: {
        "0101",                                    # 元旦
        "0128", "0129", "0130", "0131", "0203", "0204",  # 春节(1/28-2/4)
        "0404",                                    # 清明
        "0501", "0502", "0505",                    # 劳动节(5/1-5/5)
        "0602",                                    # 端午
        "1001", "1002", "1003", "1006", "1007", "1008",  # 国庆+中秋(10/1-10/8)
    },
    2026: {
        "0101", "0102",                            # 元旦(1/1-1/3)
        "0216", "0217", "0218", "0219", "0220", "0223",  # 春节(2/15-2/22,交易所实际休市至2/23,指数K线核实)
        "0406",                                    # 清明(4/4-4/6)
        "0501", "0504", "0505",                    # 劳动节(5/1-5/5)
        "0619",                                    # 端午(6/19-6/21)
        "0925",                                    # 中秋(9/25-9/27)
        "1001", "1002", "1005", "1006", "1007", "1008",  # 国庆(10/1-10/8)
    },
}


def _fallback_calendar_with_holidays(year: int) -> set:
    """降级方案: 工作日视为交易日，再剔除本地节假日表中的工作日休市。"""
    dates = _generate_fallback_calendar(year)
    holidays = _HOLIDAY_WEEKDAYS.get(year)
    if holidays:
        dates -= {f"{year}{md}" for md in holidays}
        logger.warning(
            f"交易日历降级: Baostock不可用，使用本地节假日表({year}，"
            f"剔除{len(holidays)}个工作日休市)"
        )
    else:
        logger.warning(
            f"交易日历降级: Baostock不可用且{year}不在节假日表，"
            f"回退'周一至五全是交易日'(会把法定节假日误判为交易日)"
        )
    return dates


def _load_trading_calendar(year: int = None, timeout: int = None) -> set:
    """
    加载交易日历(带缓存)

    Args:
        year: 年份, 默认当前年
        timeout: Baostock查询超时秒数; None用默认值。热路径调用方
            (如日线覆盖检查)应传短超时, 失败自动降级本地节假日表。

    Returns:
        set of str: 交易日期集合, 格式 "YYYYMMDD"
    """
    global _trading_dates_cache, _cache_date

    if year is None:
        year = _now_bj().year

    cache_key = str(year)
    if _trading_dates_cache is not None and _cache_date == cache_key:
        return _trading_dates_cache

    try:
        from data.history import query_baostock_trade_dates
        if timeout is None:
            raw = query_baostock_trade_dates(f"{year}-01-01", f"{year}-12-31")
        else:
            raw = query_baostock_trade_dates(f"{year}-01-01", f"{year}-12-31", timeout=timeout)
        data = (raw or {}).get("data") or []

        if data:
            dates = set()
            for row in data:
                # row = [calendar_date, is_trading_day]
                if row[1] == "1":
                    dates.add(row[0].replace("-", ""))
            _trading_dates_cache = dates
            _cache_date = cache_key
            logger.info(f"交易日历加载完成: {len(dates)}个交易日 (Baostock)")
            return dates
    except Exception as e:
        logger.error(f"交易日历加载失败: {e}")

    # 降级: 工作日视为交易日 + 本地节假日表剔除(不准确但可用)
    _trading_dates_cache = _fallback_calendar_with_holidays(year)
    _cache_date = cache_key
    return _trading_dates_cache


def _generate_fallback_calendar(year: int) -> set:
    """降级方案: 工作日视为交易日"""
    dates = set()
    d = datetime(year, 1, 1)
    end = datetime(year, 12, 31)
    while d <= end:
        if d.weekday() < 5:  # 周一到周五
            dates.add(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return dates


def is_trading_day(date: str = None) -> bool:
    """
    判断是否为交易日

    Args:
        date: 日期 "YYYYMMDD" 或 "YYYY-MM-DD", 默认今天

    Returns:
        bool: 是否为交易日
    """
    if date is None:
        date = _now_bj().strftime("%Y%m%d")
    date = date.replace("-", "")[:8]

    year = int(date[:4])
    calendar = _load_trading_calendar(year)
    return date in calendar


def next_trading_day(date: str = None, n: int = 1) -> str:
    """
    获取下一交易日

    Args:
        date: 起始日期, 默认今天
        n: 第几个交易日, 默认1

    Returns:
        str: 交易日期 "YYYYMMDD"
    """
    if date is None:
        date = _now_bj().strftime("%Y%m%d")
    date = date.replace("-", "")[:8]

    d = datetime.strptime(date, "%Y%m%d") + timedelta(days=1)
    count = 0
    for _ in range(30):  # 最多查30天
        if is_trading_day(d.strftime("%Y%m%d")):
            count += 1
            if count >= n:
                return d.strftime("%Y%m%d")
        d += timedelta(days=1)
    return d.strftime("%Y%m%d")


def prev_trading_day(date: str = None, n: int = 1) -> str:
    """
    获取上一交易日

    Args:
        date: 起始日期, 默认今天
        n: 第几个交易日, 默认1

    Returns:
        str: 交易日期 "YYYYMMDD"
    """
    if date is None:
        date = _now_bj().strftime("%Y%m%d")
    date = date.replace("-", "")[:8]

    d = datetime.strptime(date, "%Y%m%d") - timedelta(days=1)
    count = 0
    for _ in range(30):
        if is_trading_day(d.strftime("%Y%m%d")):
            count += 1
            if count >= n:
                return d.strftime("%Y%m%d")
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def get_market_status() -> str:
    """
    获取当前市场状态

    Returns:
        str: "盘前" / "盘中" / "午休" / "盘后" / "休市"
    """
    now = _now_bj()

    if not is_trading_day(now.strftime("%Y%m%d")):
        return "休市"

    t = now.time()
    if t < dt_time(9, 15):
        return "盘前"
    elif dt_time(9, 15) <= t < dt_time(9, 30):
        return "集合竞价"
    elif dt_time(9, 30) <= t < dt_time(11, 30):
        return "盘中"
    elif dt_time(11, 30) <= t < dt_time(13, 0):
        return "午休"
    elif dt_time(13, 0) <= t < dt_time(15, 0):
        return "盘中"
    elif dt_time(15, 0) <= t < dt_time(15, 30):
        return "盘后"
    else:
        return "盘后"


def is_market_open() -> bool:
    """判断当前是否在交易时段"""
    return get_market_status() == "盘中"


def get_trading_days(start: str, end: str) -> List[str]:
    """
    获取区间内的交易日列表

    Args:
        start: 开始日期 "YYYYMMDD"
        end: 结束日期 "YYYYMMDD"

    Returns:
        List[str]: 交易日列表
    """
    start = start.replace("-", "")[:8]
    end = end.replace("-", "")[:8]

    d = datetime.strptime(start, "%Y%m%d")
    end_d = datetime.strptime(end, "%Y%m%d")
    result = []

    while d <= end_d:
        ds = d.strftime("%Y%m%d")
        if is_trading_day(ds):
            result.append(ds)
        d += timedelta(days=1)

    return result


# 测试
if __name__ == "__main__":
    today = _now_bj().strftime("%Y%m%d")
    print(f"今天 {today}: {get_market_status()}")
    print(f"是否交易日: {is_trading_day(today)}")
    print(f"下一交易日: {next_trading_day(today)}")
    print(f"上一交易日: {prev_trading_day(today)}")
    print(f"市场是否开盘: {is_market_open()}")
