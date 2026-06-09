"""
RSI超卖反弹策略（均值回归类）
====================================================================
逻辑：RSI跌入超卖区后反弹 + 价格接近支撑位
适合震荡市抄底，趋势市容易抄在半山腰
====================================================================
"""
import pandas as pd
import numpy as np
from strategy.strategies.base import BaseStrategy, Signal


class RSIBounceStrategy(BaseStrategy):
    """RSI超卖反弹策略"""

    name = "RSI超卖反弹"
    version = "1.0"
    params = {
        "rsi_period": 14,       # RSI周期
        "rsi_oversold": 30,     # 超卖阈值
        "rsi_bounce": 35,       # 反弹确认阈值（RSI从30以下回升到此值以上）
        "bb_period": 20,        # 布林带周期
        "bb_std": 2.0,          # 布林带标准差
        "vol_ratio": 1.2,       # 反弹日放量倍数
    }

    def _calc_rsi(self, series, period):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(period, min_periods=period).mean()
        avg_loss = loss.rolling(period, min_periods=period).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def generate_signals(self, code, df, **kwargs):
        if df is None or len(df) < 60:
            return Signal(date="", code=code, action="HOLD", score=0, reason="数据不足")

        df = df.copy().sort_values("date").reset_index(drop=True)
        c = df["close"].astype(float)
        v = df["volume"].astype(float)
        p = self.params

        # RSI
        rsi = self._calc_rsi(c, p["rsi_period"])

        # 布林带
        bb_mid = c.rolling(p["bb_period"]).mean()
        bb_std = c.rolling(p["bb_period"]).std()
        bb_lower = bb_mid - p["bb_std"] * bb_std

        # 超卖反弹：RSI前几天在超卖区，今天回升到bounce以上
        was_oversold = rsi.shift(1).rolling(3).min() < p["rsi_oversold"]
        bounced = rsi >= p["rsi_bounce"]
        near_bb_lower = c <= bb_lower * 1.02  # 价格接近布林下轨

        # 放量
        vol_avg = v.rolling(20).mean()
        vol_ok = v >= vol_avg * p["vol_ratio"]

        buy_signal = was_oversold & bounced & near_bb_lower

        idx = len(df) - 1
        last_date = df["date"].iloc[idx]

        if buy_signal.iloc[idx]:
            score = 70
            if near_bb_lower.iloc[idx]:
                score += 10
            if vol_ok.iloc[idx]:
                score += 10
            return Signal(
                date=last_date, code=code, action="BUY",
                score=min(score, 100),
                reason=f"RSI反弹: RSI={rsi.iloc[idx]:.0f} 从超卖区回升+接近布林下轨",
                metadata={"rsi": float(rsi.iloc[idx]), "bb_lower": float(bb_lower.iloc[idx])},
            )

        # RSI超买卖出
        if rsi.iloc[idx] > 70 and rsi.iloc[idx] < rsi.iloc[idx - 1]:
            return Signal(
                date=last_date, code=code, action="SELL", score=60,
                reason=f"RSI超买回落: RSI={rsi.iloc[idx]:.0f}",
            )

        return Signal(date=last_date, code=code, action="HOLD", score=0, reason="无信号")
