"""
ATR（Average True Range）计算模块
用于动态止损：止损幅度随股票波动率自动调整

ATR = max(high-low, abs(high-prev_close), abs(low-prev_close)) 的N日SMA
"""
import logging
from typing import Dict, Optional
from datetime import datetime, timedelta

import pandas as pd

from config import ATR_PERIOD

logger = logging.getLogger("data.atr")


def calc_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """
    计算单只股票的ATR值

    Args:
        df: 日线数据，需包含 high, low, close 列
        period: ATR计算周期，默认14日

    Returns:
        最新ATR值，失败返回 0.0
    """
    if df is None or len(df) < period + 1:
        logger.warning(f"数据不足: 需要{period+1}条, 实际{len(df) if df is not None else 0}条")
        return 0.0

    try:
        # 确保列名统一（兼容不同数据源）
        df = df.copy()
        col_map = {}
        for col in df.columns:
            col_lower = col.lower()
            if col_lower in ("high", "最高"):
                col_map[col] = "high"
            elif col_lower in ("low", "最低"):
                col_map[col] = "low"
            elif col_lower in ("close", "收盘"):
                col_map[col] = "close"
        if col_map:
            df = df.rename(columns=col_map)

        for required in ("high", "low", "close"):
            if required not in df.columns:
                logger.warning(f"缺少 {required} 列, 无法计算ATR")
                return 0.0

        # 转为数值
        for col in ("high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["high", "low", "close"])
        if len(df) < period + 1:
            return 0.0

        # 计算True Range
        high = df["high"].values
        low = df["low"].values
        prev_close = df["close"].shift(1).values

        tr = pd.DataFrame({
            "hl": high - low,
            "hc": abs(high - prev_close),
            "lc": abs(low - prev_close),
        }).max(axis=1)

        # ATR = TR的N日SMA（取最后period条）
        atr = tr.iloc[-period:].mean()

        return float(atr) if pd.notna(atr) else 0.0

    except Exception as e:
        logger.error(f"ATR计算异常: {e}")
        return 0.0


def calc_atr_for_stocks(
    codes: list,
    get_df_func=None,
    period: int = ATR_PERIOD,
) -> Dict[str, float]:
    """
    批量计算多只股票的ATR

    Args:
        codes: 股票代码列表
        get_df_func: 获取日线数据的函数，签名 (code) -> DataFrame
                     默认使用 data.history.get_daily
        period: ATR周期

    Returns:
        {code: atr_value} 字典，失败的股票不包含在内
    """
    if get_df_func is None:
        from data.history import get_daily
        get_df_func = lambda code: get_daily(code, start_date="20240101")

    atr_map = {}
    for code in codes:
        try:
            df = get_df_func(code)
            atr = calc_atr(df, period)
            if atr > 0:
                atr_map[code] = atr
        except Exception as e:
            logger.warning(f"计算ATR失败 {code}: {e}")

    logger.info(f"ATR计算完成: {len(atr_map)}/{len(codes)}只")
    return atr_map


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # 测试
    from data.history import get_daily

    df = get_daily("600519", start_date="20240101")
    atr = calc_atr(df)
    print(f"贵州茅台 ATR(14) = {atr:.2f}")
    print(f"当前价约 {float(df.iloc[-1]['close']):.2f}")
    print(f"ATR止损距离: {atr*2:.2f} ({atr*2/float(df.iloc[-1]['close'])*100:.1f}%)")
