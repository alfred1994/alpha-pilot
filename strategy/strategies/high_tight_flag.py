"""先上涨、再缩量整理、最后突破的高窄旗形，独立实现通用形态。"""
from .base import BaseStrategy, Signal
from .pattern_data import daily_window


class HighTightFlagStrategy(BaseStrategy):
    name = "高窄旗形突破"
    version = "1.0"
    params = {"pole_bars": 30, "flag_bars": 10, "min_rise": 0.60,
              "max_range": 0.15, "max_drawdown": 0.20,
              "shrink_ratio": 0.70, "breakout_volume": 1.50}

    def __init__(self):
        self.params = type(self).params.copy()

    def generate_signals(self, code, df, **kwargs):
        p = self.params
        pole_bars, flag_bars = int(p["pole_bars"]), int(p["flag_bars"])
        if pole_bars < 2 or flag_bars < 2:
            return Signal("", code, "HOLD", 0, "形态窗口参数无效")
        frame, error = daily_window(df, pole_bars + flag_bars + 1, kwargs.get("as_of"))
        if frame is None:
            return Signal("", code, "HOLD", 0, error)
        pole = frame.iloc[:pole_bars]
        flag = frame.iloc[pole_bars:-1]
        last = frame.iloc[-1]
        rise = float(pole.close.iloc[-1] / pole.close.iloc[0] - 1)
        width = float(flag.high.max() / flag.low.min() - 1)
        drawdown = float(1 - flag.low.min() / pole.high.max())
        shrink = float(flag.volume.mean() / pole.volume.mean())
        baseline = float(frame.high.iloc[:-1].max())
        volume_ratio = float(last.volume / flag.volume.mean())
        conditions = {
            "prior_rise": rise >= p["min_rise"],
            "tight_flag": width <= p["max_range"],
            "high_level": drawdown <= p["max_drawdown"],
            "volume_contraction": shrink <= p["shrink_ratio"],
            "breakout": bool(last.close > baseline),
            "volume_confirmation": volume_ratio >= p["breakout_volume"],
        }
        metadata = {"conditions": conditions, "pole_return": rise, "flag_range": width,
                    "drawdown": drawdown, "shrink_ratio": shrink,
                    "breakout_level": baseline, "volume_ratio": volume_ratio,
                    "window_start": frame.date.iloc[0].strftime("%Y-%m-%d")}
        passed = all(conditions.values())
        return Signal(last.date.strftime("%Y-%m-%d"), code, "BUY" if passed else "HOLD",
                      80 if passed else 0,
                      "高窄旗形: 先上涨、缩量整理、放量突破" if passed else "高窄旗形条件未全部满足",
                      metadata)
