"""双通道趋势指标（DTC）：长期通道定方向，短期通道定节奏，带迟滞减少来回切换。

- 长通道：EMA(long) ± k × ATR(20)，且长中轨高于 slope_n 日前才允许转多；
- 短通道：EMA(short) ± k × ATR(20)；
- 收盘突破上轨转多，跌破下轨转空，其余时间保持原状态。

2026-10 回测结论（docs/signal-drought-audit-2026-10-29.md）：个股层面用作选股规则
显著跑输随机与等权；用于大盘仓位上限时可降低回撤但不提高收益。因此本模块只用于
影子记录的大盘仓位上限与对照池，不直接驱动下单。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series, n: int):
    return series.ewm(span=n, adjust=False, min_periods=n).mean()


def atr(high, low, close, n: int = 20):
    prev = close.shift(1)
    parts = [high - low, (high - prev).abs(), (low - prev).abs()]
    if isinstance(close, pd.DataFrame):
        tr = np.maximum(parts[0], np.maximum(parts[1], parts[2]))
    else:
        tr = pd.concat(parts, axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def hysteresis(on, off):
    """on/off 条件 -> 持续状态（Series 或 DataFrame 通用）；同时满足时以 off 为准。"""
    marks = np.where(off, 0.0, np.where(on, 1.0, np.nan))
    if isinstance(on, pd.DataFrame):
        state = pd.DataFrame(marks, index=on.index, columns=on.columns)
    else:
        state = pd.Series(marks, index=on.index)
    return state.ffill().fillna(0.0).astype(bool)


def dtc_states(high, low, close, *, long_n: int = 120, short_n: int = 20,
               k: float = 0.5, slope_n: int = 10):
    """返回 (long_on, short_on)，输入可为单序列或宽表。"""
    band = atr(high, low, close)
    mid_long, mid_short = ema(close, long_n), ema(close, short_n)
    rising = mid_long > mid_long.shift(slope_n)
    valid = mid_long.notna() & band.notna()
    long_on = hysteresis((close > mid_long + k * band) & rising & valid, close < mid_long - k * band)
    short_on = hysteresis((close > mid_short + k * band) & valid, close < mid_short - k * band)
    return long_on & valid, short_on & valid


def exposure_cap(high, low, close, *, light: float = 0.3, **kwargs):
    """仓位上限：长短通道均多头=1，仅长通道多头=light，长通道空头=0。"""
    long_on, short_on = dtc_states(high, low, close, **kwargs)
    values = np.where(long_on & short_on, 1.0, np.where(long_on, light, 0.0))
    if isinstance(close, pd.DataFrame):
        return pd.DataFrame(values, index=close.index, columns=close.columns)
    return pd.Series(values, index=close.index)
