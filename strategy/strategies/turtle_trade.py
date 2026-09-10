"""海龟突破：20日新高入场 + 成交额/阳线过滤 + 10日低点退出。

独立实现，不复制上游源码。仓位单位以 ATR 风险预算形式写入 metadata，
由调用方结合账户风险预算决定是否下单；本层不直接改账户。
"""
from .base import BaseStrategy, Signal
from .pattern_data import daily_window
import pandas as pd


class TurtleTradeStrategy(BaseStrategy):
    name = "海龟突破"
    version = "1.0"
    requires_cross_section = False
    params = {
        "entry_bars": 20,          # 入场突破窗口
        "exit_bars": 10,           # 退出窗口
        "min_amount_wan": 10000.0,  # 最低成交额（万元），1亿元
        "require_bullish": True,   # 阳线防诱多
        "atr_period": 20,
        "risk_fraction": 0.01,     # 单笔风险占账户比例
        "max_add_ons": 3,          # 额外加仓次数（总仓位最多4个单位）
        "add_on_atr": 0.5,         # 每上涨0.5N加仓一次
    }

    def __init__(self):
        self.params = type(self).params.copy()

    @staticmethod
    def _true_range(frame):
        prev_close = frame.close.shift(1)
        tr = (frame.high - frame.low).to_frame("hl")
        tr["hc"] = (frame.high - prev_close).abs()
        tr["lc"] = (frame.low - prev_close).abs()
        return tr.max(axis=1)

    @staticmethod
    def _amount_series(frame, df):
        """成交额（元）：优先原始 amount 列，否则 close*volume 粗算。"""
        if df is not None and isinstance(df, pd.DataFrame) and "amount" in df.columns:
            try:
                raw = df[["date", "amount"]].copy()
                raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
                raw = raw.dropna(subset=["date"]).sort_values("date")
                mapped = frame[["date"]].merge(raw, on="date", how="left")
                values = pd.to_numeric(mapped["amount"], errors="coerce")
                if values.notna().all() and (values > 0).all():
                    return values.reset_index(drop=True)
            except (ValueError, TypeError, KeyError):
                pass
        return (frame.close * frame.volume).reset_index(drop=True)

    def generate_signals(self, code, df, **kwargs):
        p = self.params
        entry_bars = int(p["entry_bars"])
        exit_bars = int(p["exit_bars"])
        atr_period = int(p["atr_period"])
        need = max(entry_bars, exit_bars, atr_period) + 2
        frame, error = daily_window(df, need, kwargs.get("as_of"))
        if frame is None:
            return Signal("", code, "HOLD", 0, error)

        total_assets = float(kwargs.get("total_assets") or 0)
        last = frame.iloc[-1]
        date = last.date.strftime("%Y-%m-%d")

        amount = self._amount_series(frame, df)
        min_amount = float(p["min_amount_wan"]) * 10000.0

        entry_high = float(frame.high.iloc[-entry_bars - 1:-1].max())
        exit_low = float(frame.low.iloc[-exit_bars - 1:-1].min())
        atr_series = self._true_range(frame).rolling(atr_period).mean()
        atr = float(atr_series.iloc[-1]) if len(atr_series.dropna()) else 0.0

        breakout = float(last.close) > entry_high
        amount_ok = float(amount.iloc[-1]) >= min_amount
        bullish_ok = float(last.close) >= float(last.open) if p["require_bullish"] else True
        # 退出：跌破此前 exit_bars 日最低价
        exit_trigger = float(last.close) < exit_low

        risk_budget = total_assets * float(p["risk_fraction"]) if total_assets > 0 else 0.0
        unit_shares = int(risk_budget / atr) if (atr > 0 and risk_budget > 0) else 0
        # 一手约束（A股100股）
        unit_shares = (unit_shares // 100) * 100

        stop_price = float(last.close) - atr if atr > 0 else 0.0
        add_on_levels = [
            float(last.close) + float(p["add_on_atr"]) * atr * (i + 1)
            for i in range(int(p["max_add_ons"]))
        ] if atr > 0 else []

        metadata = {
            "entry_high": entry_high,
            "exit_low": exit_low,
            "atr": atr,
            "amount": float(amount.iloc[-1]),
            "unit_shares": unit_shares,
            "risk_budget": risk_budget,
            "stop_price": stop_price,
            "add_on_levels": add_on_levels,
            "max_units": 1 + int(p["max_add_ons"]),
            "conditions": {
                "breakout": breakout,
                "amount_filter": amount_ok,
                "bullish_filter": bullish_ok,
                "exit_break": exit_trigger,
            },
            "window_start": frame.date.iloc[0].strftime("%Y-%m-%d"),
        }

        if exit_trigger and not breakout:
            return Signal(
                date, code, "SELL", 70,
                f"海龟退出: 跌破{exit_bars}日低点 {exit_low:.2f}",
                metadata,
            )
        if breakout and amount_ok and bullish_ok:
            return Signal(
                date, code, "BUY", 82,
                "海龟突破: 收盘新高+成交额达标+阳线确认",
                metadata,
            )
        reasons = []
        if not breakout:
            reasons.append(f"未突破{entry_bars}日高点")
        if not amount_ok:
            reasons.append("成交额不足")
        if not bullish_ok:
            reasons.append("非阳线确认")
        return Signal(
            date, code, "HOLD", 0,
            "海龟条件未满足: " + "、".join(reasons) if reasons else "海龟条件未满足",
            metadata,
        )

    def position_plan(self, total_assets: float, price: float = None) -> dict:
        """给出1个单位的风险预算与股数建议；不含加仓执行。"""
        risk = float(total_assets or 0) * float(self.params["risk_fraction"])
        return {
            "risk_fraction": self.params["risk_fraction"],
            "risk_budget": risk,
            "max_units": 1 + int(self.params["max_add_ons"]),
            "add_on_atr": self.params["add_on_atr"],
            "price": price,
        }
