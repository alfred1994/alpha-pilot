"""
平台突破策略（横盘突破类）
====================================================================
逻辑：价格在窄幅震荡区间内整理N天后向上突破
适合捕捉蓄力后的启动行情
====================================================================
"""
import pandas as pd
import numpy as np
from strategy.strategies.base import BaseStrategy, Signal


class PlatformBreakoutStrategy(BaseStrategy):
    """平台突破策略"""

    name = "平台突破"
    version = "1.0"
    params = {
        "consolidation_days": 15,   # 横盘整理最少天数
        "range_pct": 0.08,          # 整理区间振幅上限（8%以内视为横盘）
        "breakout_pct": 0.02,       # 突破幅度（高于区间上沿2%）
        "vol_ratio": 1.5,           # 突破日放量倍数
        "vol_ma_period": 20,        # 均量周期
    }

    def generate_signals(self, code, df, **kwargs):
        if df is None or len(df) < 60:
            return Signal(date="", code=code, action="HOLD", score=0, reason="数据不足")

        df = df.copy().sort_values("date").reset_index(drop=True)
        c = df["close"].astype(float)
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        v = df["volume"].astype(float)
        p = self.params

        n = p["consolidation_days"]

        # 计算过去N天的区间
        high_n = h.rolling(n).max()
        low_n = l.rolling(n).min()
        range_pct = (high_n - low_n) / low_n

        # 横盘条件：区间振幅≤range_pct
        is_consolidation = range_pct <= p["range_pct"]

        # 突破条件：今天收盘价超过区间上沿
        breakout = c > high_n.shift(1) * (1 + p["breakout_pct"])

        # 放量确认
        vol_avg = v.rolling(p["vol_ma_period"]).mean()
        vol_ok = v >= vol_avg * p["vol_ratio"]

        buy_signal = is_consolidation.shift(1) & breakout & vol_ok

        idx = len(df) - 1
        last_date = df["date"].iloc[idx]

        if buy_signal.iloc[idx]:
            score = 75
            if range_pct.shift(1).iloc[idx] < 0.05:
                score += 10  # 越窄的平台突破越有效
            return Signal(
                date=last_date, code=code, action="BUY",
                score=min(score, 100),
                reason=f"平台突破: 整理{n}天(振幅{range_pct.shift(1).iloc[idx]:.1%})后放量突破",
                metadata={
                    "consolidation_days": n,
                    "range_pct": float(range_pct.shift(1).iloc[idx]) if not pd.isna(range_pct.shift(1).iloc[idx]) else 0,
                    "vol_ratio": float(v.iloc[idx] / vol_avg.iloc[idx]) if vol_avg.iloc[idx] > 0 else 0,
                },
            )

        # 跌破整理区间下沿卖出
        support = low_n.shift(1)
        if c.iloc[idx] < support.iloc[idx] * 0.98 and not pd.isna(support.iloc[idx]):
            return Signal(
                date=last_date, code=code, action="SELL", score=55,
                reason=f"跌破平台: 价格{c.iloc[idx]:.2f} < 平台下沿{support.iloc[idx]:.2f}",
            )

        return Signal(date=last_date, code=code, action="HOLD", score=0, reason="无信号")
