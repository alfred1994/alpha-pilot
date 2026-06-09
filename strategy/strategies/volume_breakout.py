"""
量价突破策略（突破类）
====================================================================
逻辑：价格突破N日新高 + 成交量放大确认
适合捕捉启动行情，追涨有一定风险
====================================================================
"""
import pandas as pd
import numpy as np
from strategy.strategies.base import BaseStrategy, Signal


class VolumeBreakoutStrategy(BaseStrategy):
    """量价突破策略"""

    name = "量价突破"
    version = "1.0"
    params = {
        "lookback": 20,         # 突破回看天数
        "vol_ratio": 2.0,       # 突破日成交量/20日均量 倍数
        "close_pct": 0.02,      # 收盘价需高于前高至少2%
        "atr_period": 14,       # ATR周期（用于止损参考）
        "max_atr_pct": 0.08,    # ATR不超过价格8%（过滤暴涨股）
    }

    def generate_signals(self, code, df, **kwargs):
        if df is None or len(df) < 60:
            return Signal(date="", code=code, action="HOLD", score=0, reason="数据不足")

        df = df.copy().sort_values("date").reset_index(drop=True)
        c = df["close"].astype(float)
        h = df["high"].astype(float)
        v = df["volume"].astype(float)
        p = self.params

        # N日最高价（不含今天）
        prev_high = h.rolling(p["lookback"]).max().shift(1)
        # 突破
        breakout = c > prev_high * (1 + p["close_pct"])
        # 放量确认
        vol_avg = v.rolling(20).mean()
        vol_ok = v >= vol_avg * p["vol_ratio"]

        # ATR过滤（排除波动过大的股票）
        tr = pd.concat([
            h - df["low"].astype(float),
            (h - c.shift(1)).abs(),
            (df["low"].astype(float) - c.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(p["atr_period"]).mean()
        atr_pct = atr / c
        atr_ok = atr_pct <= p["max_atr_pct"]

        buy_signal = breakout & vol_ok & atr_ok

        idx = len(df) - 1
        last_date = df["date"].iloc[idx]

        if buy_signal.iloc[idx]:
            score = 75
            if v.iloc[idx] > vol_avg.iloc[idx] * 3:
                score += 10  # 3倍量以上加分
            return Signal(
                date=last_date, code=code, action="BUY",
                score=min(score, 100),
                reason=f"量价突破: 突破{p['lookback']}日新高+成交量放大{v.iloc[idx]/vol_avg.iloc[idx]:.1f}倍",
                metadata={
                    "prev_high": float(prev_high.iloc[idx]) if not pd.isna(prev_high.iloc[idx]) else 0,
                    "vol_ratio": float(v.iloc[idx] / vol_avg.iloc[idx]) if vol_avg.iloc[idx] > 0 else 0,
                },
            )

        # 跌破突破前高点的支撑位卖出
        support = prev_high * 0.97  # 前高下浮3%作为支撑
        if c.iloc[idx] < support.iloc[idx] and not pd.isna(support.iloc[idx]):
            return Signal(
                date=last_date, code=code, action="SELL", score=55,
                reason=f"跌破支撑: 价格{c.iloc[idx]:.2f} < 支撑位{support.iloc[idx]:.2f}",
            )

        return Signal(date=last_date, code=code, action="HOLD", score=0, reason="无信号")
