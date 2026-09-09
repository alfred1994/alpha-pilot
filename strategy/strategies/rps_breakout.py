"""日期对齐的横截面相对强度与前高突破；缺失市场样本时不生成买入。"""
import numpy as np
import pandas as pd
from .base import BaseStrategy, Signal
from .pattern_data import daily_window


class RPSBreakoutStrategy(BaseStrategy):
    name = "横截面RPS突破"
    version = "1.0"
    requires_cross_section = True
    params = {"period": 60, "rps_threshold": 90.0, "breakout_bars": 20,
              "min_universe": 20}

    def __init__(self):
        self.params = type(self).params.copy()

    def generate_signals(self, code, df, **kwargs):
        p = self.params
        period, breakout_bars = int(p["period"]), int(p["breakout_bars"])
        if period < 1 or breakout_bars < 1 or int(p["min_universe"]) < 2:
            return Signal("", code, "HOLD", 0, "RPS 参数无效")
        frame, error = daily_window(df, max(period, breakout_bars) + 1, kwargs.get("as_of"))
        if frame is None:
            return Signal("", code, "HOLD", 0, error)
        date = frame.date.iloc[-1].strftime("%Y-%m-%d")
        panel = kwargs.get("universe_closes")
        if not isinstance(panel, pd.DataFrame) or panel.empty:
            return Signal(date, code, "HOLD", 0, "缺少 point-in-time 横截面收盘价")
        try:
            panel = panel.copy()
            panel.index = pd.to_datetime(panel.index, errors="raise").normalize()
            panel = panel[panel.index <= frame.date.iloc[-1]]
            if panel.index.isna().any() or panel.index.duplicated().any() or panel.columns.duplicated().any():
                raise ValueError("duplicate/missing keys")
            dates = frame.date.iloc[-period - 1:]
            aligned = panel.reindex(pd.DatetimeIndex(dates)).apply(pd.to_numeric, errors="coerce")
            # 不前向填充停牌/缺失日，所有收益使用相同起止日和完整窗口。
            valid = np.isfinite(aligned).all(axis=0) & (aligned > 0).all(axis=0)
            aligned = aligned.loc[:, valid]
            if code not in aligned or not np.allclose(
                    aligned[code].to_numpy(), frame.close.iloc[-period - 1:].to_numpy(), rtol=1e-6):
                return Signal(date, code, "HOLD", 0, "目标股票横截面缺失或复权口径不一致")
            count = len(aligned.columns)
            if count < int(p["min_universe"]):
                return Signal(date, code, "HOLD", 0, "日期对齐的横截面样本不足",
                              {"universe_size": count})
            returns = aligned.iloc[-1] / aligned.iloc[0] - 1
            rps = float(returns.rank(method="average", pct=True)[code] * 100)
        except (ValueError, TypeError, OverflowError):
            return Signal(date, code, "HOLD", 0, "横截面日期或价格无效")
        baseline = float(frame.high.iloc[-breakout_bars - 1:-1].max())
        conditions = {"relative_strength": rps >= p["rps_threshold"],
                      "breakout": bool(frame.close.iloc[-1] > baseline)}
        passed = all(conditions.values())
        return Signal(date, code, "BUY" if passed else "HOLD", rps if passed else 0,
                      f"横截面RPS {rps:.1f}: " + ("强势且突破前高" if passed else "条件未全部满足"),
                      {"conditions": conditions, "rps": rps, "universe_size": count,
                       "period_return": float(returns[code]), "breakout_level": baseline,
                       "window_start": dates.iloc[0].strftime("%Y-%m-%d")})
