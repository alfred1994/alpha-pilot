"""
均线金叉策略（趋势跟踪类）
====================================================================
逻辑：短期均线上穿长期均线 + 价格站上趋势线
适合趋势行情，震荡市容易假信号
====================================================================
"""
import pandas as pd
import numpy as np
from strategy.strategies.base import BaseStrategy, Signal


class MACrossStrategy(BaseStrategy):
    """均线金叉策略"""

    name = "均线金叉"
    version = "1.0"
    params = {
        "ma_short": 5,          # 短期均线
        "ma_long": 20,          # 长期均线
        "ma_trend": 60,         # 趋势均线（价格需站上此线）
        "vol_ratio": 1.5,       # 放量倍数（当日量/20日均量）
        "min_pct_above": 0.01,  # 价格高于长期均线至少1%
    }

    def generate_signals(self, code, df, **kwargs):
        if df is None or len(df) < 60:
            return Signal(date="", code=code, action="HOLD", score=0, reason="数据不足")

        df = df.copy().sort_values("date").reset_index(drop=True)
        c = df["close"].astype(float)
        v = df["volume"].astype(float)
        p = self.params

        ma_s = c.rolling(p["ma_short"]).mean()
        ma_l = c.rolling(p["ma_long"]).mean()
        ma_t = c.rolling(p["ma_trend"]).mean()
        vol_avg = v.rolling(20).mean()

        # 金叉：短均线从下穿上长均线
        cross = (ma_s > ma_l) & (ma_s.shift(1) <= ma_l.shift(1))
        # 价格站上趋势线
        above_trend = c >= ma_t
        # 放量
        vol_ok = v >= vol_avg * p["vol_ratio"]
        # 价格高于长期均线一定幅度
        pct_above = (c - ma_l) / ma_l >= p["min_pct_above"]

        buy_signal = cross & above_trend & vol_ok & pct_above

        idx = len(df) - 1
        last_date = df["date"].iloc[idx]

        if buy_signal.iloc[idx]:
            score = 70
            if pct_above.iloc[idx] > 0.03:
                score += 10
            if vol_ok.iloc[idx]:
                score += 10
            return Signal(
                date=last_date, code=code, action="BUY",
                score=min(score, 100),
                reason=f"均线金叉: MA{p['ma_short']}上穿MA{p['ma_long']}+放量+趋势线上方",
            )

        # 死叉卖出信号
        death_cross = (ma_s < ma_l) & (ma_s.shift(1) >= ma_l.shift(1))
        if death_cross.iloc[idx]:
            return Signal(
                date=last_date, code=code, action="SELL", score=60,
                reason=f"均线死叉: MA{p['ma_short']}下穿MA{p['ma_long']}",
            )

        return Signal(date=last_date, code=code, action="HOLD", score=0, reason="无信号")
