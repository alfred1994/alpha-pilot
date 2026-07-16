"""
策略库
====================================================================
每个策略是一个独立的买入/卖出信号生成器。
通过 StrategyOptimizer 让LLM自动调参优化。
====================================================================
"""
from strategy.strategies.zt_reversal import ZTReversalStrategy
from strategy.strategies.ma_cross import MACrossStrategy
from strategy.strategies.rsi_bounce import RSIBounceStrategy
from strategy.strategies.volume_breakout import VolumeBreakoutStrategy
from strategy.strategies.limit_down_reversal import LimitDownReversalStrategy
from strategy.strategies.platform_breakout import PlatformBreakoutStrategy
from strategy.strategies.macd_trend import MACDTrendStrategy
from strategy.strategies.bollinger_squeeze import BollingerSqueezeStrategy
from strategy.strategies.kdj_reversal import KDJReversalStrategy
from strategy.strategies.technical_ensemble import TechnicalEnsembleStrategy

# 策略注册表
STRATEGY_REGISTRY = {
    "zt_reversal": ZTReversalStrategy,
    "ma_cross": MACrossStrategy,
    "rsi_bounce": RSIBounceStrategy,
    "volume_breakout": VolumeBreakoutStrategy,
    "limit_down_reversal": LimitDownReversalStrategy,
    "platform_breakout": PlatformBreakoutStrategy,
    "macd_trend": MACDTrendStrategy,
    "bollinger_squeeze": BollingerSqueezeStrategy,
    "kdj_reversal": KDJReversalStrategy,
    "technical_ensemble": TechnicalEnsembleStrategy,
}

# 策略画像用于发现、回测选择和人工复盘；不会改变账户、风控或执行规则。
STRATEGY_PROFILES = {
    "zt_reversal": {"category": "形态反转", "regimes": ["rebound", "sideways"], "risk": "high"},
    "ma_cross": {"category": "趋势跟踪", "regimes": ["bull", "rebound"], "risk": "medium"},
    "rsi_bounce": {"category": "均值回归", "regimes": ["sideways", "rebound"], "risk": "medium"},
    "volume_breakout": {"category": "量价突破", "regimes": ["bull", "rebound"], "risk": "medium"},
    "limit_down_reversal": {"category": "情绪反转", "regimes": ["rebound"], "risk": "high"},
    "platform_breakout": {"category": "平台突破", "regimes": ["bull", "sideways"], "risk": "medium"},
    "macd_trend": {"category": "趋势跟踪", "regimes": ["bull", "rebound"], "risk": "medium"},
    "bollinger_squeeze": {"category": "波动突破", "regimes": ["bull", "sideways", "rebound"], "risk": "medium"},
    "kdj_reversal": {"category": "均值回归", "regimes": ["sideways", "rebound"], "risk": "medium"},
    "technical_ensemble": {"category": "多策略共识", "regimes": ["bull", "bear", "sideways", "rebound"], "risk": "low"},
}


def get_strategy(name: str):
    """获取策略实例"""
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"未知策略: {name}, 可用: {list(STRATEGY_REGISTRY.keys())}")
    return cls()


def list_strategies():
    """列出所有策略"""
    result = []
    for name, cls in STRATEGY_REGISTRY.items():
        s = cls()
        result.append({
            "name": name,
            "display": s.name,
            "version": s.version,
            "params": s.params,
            **STRATEGY_PROFILES.get(name, {}),
        })
    return result
