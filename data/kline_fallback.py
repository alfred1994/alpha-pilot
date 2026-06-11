"""
长桥K线数据降级包装 - 多数据源冗余
====================================================================
优先长桥 → 降级mootdx → 失败抛异常
"""
import logging
from typing import List, Dict, Optional

logger = logging.getLogger("data.kline_multi_source")


def get_kline_with_fallback(code: str, start_date: str, end_date: str) -> List[Dict]:
    """
    多数据源K线获取（自动降级）

    优先级：
    1. 长桥API（你的主力数据源）
    2. mootdx（通达信TCP，不封IP）

    Args:
        code: 股票代码
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD

    Returns:
        K线数据列表

    Raises:
        Exception: 所有数据源失败
    """
    # 优先长桥
    try:
        from data.history import get_daily
        df = get_daily(code, start_date=start_date.replace("-", ""),
                       end_date=end_date.replace("-", ""))
        if df is not None and not df.empty:
            logger.info(f"K线获取成功(长桥): {code} {len(df)}条")
            return df.to_dict("records")
    except Exception as e:
        logger.warning(f"长桥K线失败: {code} - {e}")

    # 降级mootdx
    try:
        from data.a_stock_data import get_kline_mootdx
        result = get_kline_mootdx(code, start_date, end_date)
        if result:
            logger.info(f"K线获取成功(mootdx降级): {code} {len(result)}条")
            return result
    except Exception as e:
        logger.warning(f"mootdx K线失败: {code} - {e}")

    raise Exception(f"所有数据源失败: {code}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    kline = get_kline_with_fallback("600519", "2026-05-01", "2026-06-11")
    print(f"获取K线: {len(kline)}条")
    print(f"最新: {kline[-1]}")
