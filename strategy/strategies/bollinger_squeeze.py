"""
布林带收敛突破策略
====================================================================
逻辑：先识别低波动收敛，再用放量突破上轨确认启动；跌破中轨时退出。
适合盘整后突破，不在带宽扩张后期追逐高波动。
====================================================================
"""
import pandas as pd

from strategy.strategies.base import BaseStrategy, Signal


class BollingerSqueezeStrategy(BaseStrategy):
    """布林带收敛后突破的趋势启动策略。"""

    name = "布林收敛突破"
    version = "1.0"
    params = {
        "period": 20,
        "std_multiplier": 2.0,
        "squeeze_window": 60,
        "squeeze_quantile": 0.25,
        "volume_period": 20,
        "volume_ratio": 1.3,
    }

    def generate_signals(self, code: str, df: pd.DataFrame, **kwargs) -> Signal:
        required = {"date", "close", "volume"}
        if df is None or len(df) < 90 or not required.issubset(df.columns):
            return Signal(date="", code=code, action="HOLD", score=0, reason="数据不足")

        data = df.copy().sort_values("date").reset_index(drop=True)
        close = pd.to_numeric(data["close"], errors="coerce").ffill()
        volume = pd.to_numeric(data["volume"], errors="coerce").fillna(0)
        p = self.params
        middle = close.rolling(p["period"]).mean()
        std = close.rolling(p["period"]).std()
        upper = middle + p["std_multiplier"] * std
        lower = middle - p["std_multiplier"] * std
        width = (upper - lower) / middle
        squeeze_threshold = width.rolling(p["squeeze_window"]).quantile(p["squeeze_quantile"])
        volume_ma = volume.rolling(p["volume_period"]).mean()

        index = len(data) - 1
        last_date = data["date"].iloc[index]
        previous_squeeze = width.iloc[index - 1] <= squeeze_threshold.iloc[index - 1]
        breakout = close.iloc[index] > upper.iloc[index - 1]
        volume_ok = volume.iloc[index] >= volume_ma.iloc[index] * p["volume_ratio"]
        breakdown = close.iloc[index] < middle.iloc[index]
        metadata = {
            "band_width": round(float(width.iloc[index]), 4),
            "squeeze_threshold": round(float(squeeze_threshold.iloc[index]), 4),
            "upper": round(float(upper.iloc[index]), 4),
            "middle": round(float(middle.iloc[index]), 4),
            "lower": round(float(lower.iloc[index]), 4),
        }

        if previous_squeeze and breakout and volume_ok:
            return Signal(
                date=last_date,
                code=code,
                action="BUY",
                score=80,
                reason="布林带低波动收敛后放量突破上轨",
                metadata=metadata,
            )
        if breakdown:
            return Signal(
                date=last_date,
                code=code,
                action="SELL",
                score=55,
                reason="收盘跌破布林中轨，突破动能减弱",
                metadata=metadata,
            )
        return Signal(date=last_date, code=code, action="HOLD", score=0, reason="布林收敛突破条件未满足", metadata=metadata)
