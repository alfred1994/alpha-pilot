"""上升趋势中的跌停反包：趋势未破坏时的恐慌洗盘反转。"""
from .base import BaseStrategy, Signal
from .pattern_data import daily_window


class UptrendLimitDownStrategy(BaseStrategy):
    name = "上升趋势跌停反包"
    version = "1.0"
    requires_cross_section = False
    params = {
        "trend_bars": 60,          # 趋势评估窗口
        "ma_fast": 20,
        "ma_slow": 60,
        "min_trend_rise": 0.08,    # 近60日至少上涨8%
        "limit_down_pct": -0.095,  # 视为跌停
        "max_drop_pct": 0.18,       # 从高点最大回撤（正数）
        "engulf_min_body": 0.02,   # 反包日实体至少2%
        "require_above_ma": True,
    }

    def __init__(self):
        self.params = type(self).params.copy()

    def generate_signals(self, code, df, **kwargs):
        p = self.params
        need = max(int(p["trend_bars"]), int(p["ma_slow"])) + 3
        frame, error = daily_window(df, need, kwargs.get("as_of"))
        if frame is None:
            return Signal("", code, "HOLD", 0, error)

        close = frame.close
        open_ = frame.open
        high = frame.high
        low = frame.low
        volume = frame.volume
        date = frame.date.iloc[-1].strftime("%Y-%m-%d")

        ma_fast = close.rolling(int(p["ma_fast"])).mean()
        ma_slow = close.rolling(int(p["ma_slow"])).mean()
        if len(frame) < int(p["ma_slow"]) + 2:
            return Signal(date, code, "HOLD", 0, "均线窗口不足")

        pct = close / close.shift(1) - 1
        limit_down = pct <= p["limit_down_pct"]

        # 反包日（最新一根）：阳线吞没前一根跌停实体
        prev = frame.iloc[-2]
        last = frame.iloc[-1]
        prev_limit_down = bool(limit_down.iloc[-2])
        prev_body_top = float(max(prev.open, prev.close))
        prev_body_bottom = float(min(prev.open, prev.close))
        last_body = float(last.close - last.open)
        last_body_pct = last_body / float(last.open) if float(last.open) > 0 else 0.0
        engulfs = (
            float(last.close) > prev_body_top
            and float(last.open) <= prev_body_bottom * 1.002
            and last_body_pct >= p["engulf_min_body"]
        )
        volume_ok = float(last.volume) >= float(prev.volume) * 0.8

        rise = float(close.iloc[-1] / close.iloc[-int(p["trend_bars"])] - 1)
        drawdown_from_high = float(1 - last.close / high.iloc[:-1].max())
        trend_ok = (
            float(ma_fast.iloc[-1]) > float(ma_slow.iloc[-1])
            and rise >= p["min_trend_rise"]
            and drawdown_from_high <= p["max_drop_pct"]
        )
        price_structure_ok = (
            float(last.close) >= float(ma_fast.iloc[-1])
            if p["require_above_ma"] else True
        )

        conditions = {
            "prior_uptrend": trend_ok,
            "limit_down_shakeout": prev_limit_down,
            "bullish_engulfing": engulfs,
            "volume_support": volume_ok,
            "above_trend_ma": price_structure_ok,
        }
        metadata = {
            "conditions": conditions,
            "trend_rise": rise,
            "drawdown_from_high": drawdown_from_high,
            "prev_pct": float(pct.iloc[-2]) if prev_limit_down else 0.0,
            "engulf_body_pct": last_body_pct,
            "ma_fast": float(ma_fast.iloc[-1]),
            "ma_slow": float(ma_slow.iloc[-1]),
            "volume_ratio": float(last.volume / prev.volume) if float(prev.volume) > 0 else 0.0,
            "window_start": frame.date.iloc[0].strftime("%Y-%m-%d"),
        }
        passed = all(conditions.values())
        return Signal(
            date,
            code,
            "BUY" if passed else "HOLD",
            78 if passed else 0,
            "上升趋势跌停反包: 趋势未破+跌停洗盘+阳线吞没" if passed else "上升趋势跌停反包条件未全部满足",
            metadata,
        )
