"""
技术策略组合器
====================================================================
将趋势、突破和均值回归信号做投票，只有多个独立方法一致时才输出交易建议。
组合器不绕过账户、风控或 TradePlan，只提供可回测的候选信号。
====================================================================
"""
import pandas as pd

from strategy.strategies.base import BaseStrategy, Signal
from strategy.strategies.bollinger_squeeze import BollingerSqueezeStrategy
from strategy.strategies.kdj_reversal import KDJReversalStrategy
from strategy.strategies.ma_cross import MACrossStrategy
from strategy.strategies.macd_trend import MACDTrendStrategy
from strategy.strategies.rsi_bounce import RSIBounceStrategy
from strategy.strategies.volume_breakout import VolumeBreakoutStrategy


class TechnicalEnsembleStrategy(BaseStrategy):
    """多策略一致性筛选器，降低单一指标误触发概率。"""

    name = "技术策略组合"
    version = "1.0"
    params = {
        "buy_min_votes": 2,
        "sell_min_votes": 2,
        "min_component_score": 60,
    }

    def __init__(self):
        self._components = [
            (MACrossStrategy(), "trend"),
            (MACDTrendStrategy(), "trend"),
            (VolumeBreakoutStrategy(), "breakout"),
            (BollingerSqueezeStrategy(), "breakout"),
            (RSIBounceStrategy(), "reversal"),
            (KDJReversalStrategy(), "reversal"),
        ]

    def generate_signals(self, code: str, df: pd.DataFrame, **kwargs) -> Signal:
        if df is None or len(df) < 90:
            return Signal(date="", code=code, action="HOLD", score=0, reason="数据不足")

        market_regime = kwargs.get("market_regime", "sideways")
        component_signals = [
            (component, category, component.generate_signals(code, df, **kwargs))
            for component, category in self._components
        ]
        p = self.params
        buy_signals = [
            item for _, _, item in component_signals
            if item.action == "BUY" and item.score >= p["min_component_score"]
        ]
        sell_signals = [
            item for _, _, item in component_signals
            if item.action == "SELL" and item.score >= p["min_component_score"]
        ]
        required_buy_votes = p["buy_min_votes"] + (1 if market_regime == "bear" else 0)
        required_sell_votes = p["sell_min_votes"]
        last_date = df.sort_values("date").iloc[-1]["date"]
        metadata = {
            "components": [
                {"strategy": component.name, "action": item.action, "score": item.score, "reason": item.reason}
                for component, _, item in component_signals
            ],
            "buy_votes": len(buy_signals),
            "sell_votes": len(sell_signals),
            "market_regime": market_regime,
            "required_buy_votes": required_buy_votes,
        }

        if len(buy_signals) >= required_buy_votes and len(buy_signals) > len(sell_signals):
            score = sum(item.score for item in buy_signals) / len(buy_signals)
            names = "、".join(item.reason for item in buy_signals[:3])
            return Signal(date=last_date, code=code, action="BUY", score=round(score, 1), reason=f"多策略买入共识: {names}", metadata=metadata)
        if len(sell_signals) >= required_sell_votes and len(sell_signals) >= len(buy_signals):
            score = sum(item.score for item in sell_signals) / len(sell_signals)
            names = "、".join(item.reason for item in sell_signals[:3])
            return Signal(date=last_date, code=code, action="SELL", score=round(score, 1), reason=f"多策略卖出共识: {names}", metadata=metadata)
        return Signal(date=last_date, code=code, action="HOLD", score=0, reason="技术策略未形成足够共识", metadata=metadata)
