"""
KDJ 超卖反转策略
====================================================================
逻辑：K 线上穿 D 线且位于低位，结合短期趋势过滤确认反转；高位死叉退出。
用于捕捉震荡行情中的超卖反弹，避免把 KDJ 单独作为趋势追涨依据。
====================================================================
"""
import pandas as pd

from strategy.strategies.base import BaseStrategy, Signal


class KDJReversalStrategy(BaseStrategy):
    """KDJ 低位金叉与高位死叉策略。"""

    name = "KDJ超卖反转"
    version = "1.0"
    params = {
        "lookback": 9,
        "k_smooth": 3,
        "d_smooth": 3,
        "oversold": 30,
        "overbought": 80,
        "trend_ma": 20,
    }

    def generate_signals(self, code: str, df: pd.DataFrame, **kwargs) -> Signal:
        required = {"date", "high", "low", "close"}
        if df is None or len(df) < 40 or not required.issubset(df.columns):
            return Signal(date="", code=code, action="HOLD", score=0, reason="数据不足")

        data = df.copy().sort_values("date").reset_index(drop=True)
        high = pd.to_numeric(data["high"], errors="coerce").ffill()
        low = pd.to_numeric(data["low"], errors="coerce").ffill()
        close = pd.to_numeric(data["close"], errors="coerce").ffill()
        p = self.params

        lowest = low.rolling(p["lookback"]).min()
        highest = high.rolling(p["lookback"]).max()
        rsv = ((close - lowest) / (highest - lowest).replace(0, float("nan")) * 100).fillna(50)
        k_value = rsv.ewm(com=p["k_smooth"] - 1, adjust=False).mean()
        d_value = k_value.ewm(com=p["d_smooth"] - 1, adjust=False).mean()
        j_value = 3 * k_value - 2 * d_value
        trend_ma = close.rolling(p["trend_ma"]).mean()

        index = len(data) - 1
        last_date = data["date"].iloc[index]
        golden_cross = k_value.iloc[index - 1] <= d_value.iloc[index - 1] and k_value.iloc[index] > d_value.iloc[index]
        death_cross = k_value.iloc[index - 1] >= d_value.iloc[index - 1] and k_value.iloc[index] < d_value.iloc[index]
        low_zone = min(k_value.iloc[index], d_value.iloc[index]) <= p["oversold"]
        high_zone = max(k_value.iloc[index], d_value.iloc[index]) >= p["overbought"]
        trend_ok = close.iloc[index] >= trend_ma.iloc[index] * 0.97
        metadata = {
            "k": round(float(k_value.iloc[index]), 2),
            "d": round(float(d_value.iloc[index]), 2),
            "j": round(float(j_value.iloc[index]), 2),
            "trend_ma": round(float(trend_ma.iloc[index]), 4),
        }

        if golden_cross and low_zone and trend_ok:
            return Signal(
                date=last_date,
                code=code,
                action="BUY",
                score=72,
                reason="KDJ低位金叉，价格未明显跌破短期趋势",
                metadata=metadata,
            )
        if death_cross and high_zone:
            return Signal(
                date=last_date,
                code=code,
                action="SELL",
                score=65,
                reason="KDJ高位死叉，短线动能转弱",
                metadata=metadata,
            )
        return Signal(date=last_date, code=code, action="HOLD", score=0, reason="KDJ反转条件未满足", metadata=metadata)
