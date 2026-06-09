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

# 策略注册表
STRATEGY_REGISTRY = {
    "zt_reversal": ZTReversalStrategy,
    "ma_cross": MACrossStrategy,
    "rsi_bounce": RSIBounceStrategy,
    "volume_breakout": VolumeBreakoutStrategy,
    "limit_down_reversal": LimitDownReversalStrategy,
    "platform_breakout": PlatformBreakoutStrategy,
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
        result.append({"name": name, "display": s.name, "version": s.version, "params": s.params})
    return result
