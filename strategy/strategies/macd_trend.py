"""
MACD 趋势跟踪策略
====================================================================
逻辑：MACD 金叉/死叉结合零轴位置、均线趋势与成交量确认。
适合趋势明确的行情，避免仅凭零轴下的弱反弹追涨。
====================================================================
"""
import pandas as pd

from strategy.strategies.base import BaseStrategy, Signal


class MACDTrendStrategy(BaseStrategy):
    """MACD 金叉配合趋势过滤的交易策略。"""

    name = "MACD趋势跟踪"
    version = "1.0"
    params = {
        "fast": 12,
        "slow": 26,
        "signal": 9,
        "trend_ma": 60,
        "volume_period": 20,
        "volume_ratio": 1.1,
    }

    def generate_signals(self, code: str, df: pd.DataFrame, **kwargs) -> Signal:
        required = {"date", "close", "volume"}
        if df is None or len(df) < 70 or not required.issubset(df.columns):
            return Signal(date="", code=code, action="HOLD", score=0, reason="数据不足")

        data = df.copy().sort_values("date").reset_index(drop=True)
        close = pd.to_numeric(data["close"], errors="coerce").ffill()
        volume = pd.to_numeric(data["volume"], errors="coerce").fillna(0)
        p = self.params

        ema_fast = close.ewm(span=p["fast"], adjust=False).mean()
        ema_slow = close.ewm(span=p["slow"], adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=p["signal"], adjust=False).mean()
        histogram = (dif - dea) * 2
        trend_ma = close.rolling(p["trend_ma"]).mean()
        volume_ma = volume.rolling(p["volume_period"]).mean()

        index = len(data) - 1
        last_date = data["date"].iloc[index]
        golden_cross = dif.iloc[index - 1] <= dea.iloc[index - 1] and dif.iloc[index] > dea.iloc[index]
        death_cross = dif.iloc[index - 1] >= dea.iloc[index - 1] and dif.iloc[index] < dea.iloc[index]
        trend_ok = close.iloc[index] >= trend_ma.iloc[index]
        volume_ok = volume.iloc[index] >= volume_ma.iloc[index] * p["volume_ratio"]
        above_zero = dif.iloc[index] > 0

        metadata = {
            "dif": round(float(dif.iloc[index]), 4),
            "dea": round(float(dea.iloc[index]), 4),
            "histogram": round(float(histogram.iloc[index]), 4),
            "trend_ma": round(float(trend_ma.iloc[index]), 4),
            "volume_ratio": round(float(volume.iloc[index] / volume_ma.iloc[index]), 3)
            if volume_ma.iloc[index] > 0 else 0.0,
        }

        if golden_cross and trend_ok and volume_ok:
            score = 72 + (8 if above_zero else 0) + (6 if histogram.iloc[index] > 0 else 0)
            return Signal(
                date=last_date,
                code=code,
                action="BUY",
                score=min(score, 100),
                reason="MACD金叉，价格站上趋势均线且量能确认",
                metadata=metadata,
            )
        if death_cross or (close.iloc[index] < trend_ma.iloc[index] and histogram.iloc[index] < 0):
            return Signal(
                date=last_date,
                code=code,
                action="SELL",
                score=65 if death_cross else 55,
                reason="MACD死叉或跌破趋势均线且动能转弱",
                metadata=metadata,
            )
        return Signal(date=last_date, code=code, action="HOLD", score=0, reason="MACD趋势条件未满足", metadata=metadata)
