"""
策略配置中心
====================================================================
根据市场环境（bull/bear/sideways/rebound）选择不同的策略参数。

核心理念:
  - 牛市: 进攻型 → 放宽买入门槛, 提高仓位上限, 放宽止损
  - 熊市: 防守型 → 收紧买入门槛, 降低仓位, 收紧止损
  - 震荡: 平衡型 → 中性参数, 注重止损纪律
  - 反弹: 谨慎型 → 中性偏保守, 快速止盈

所有参数都有上下界，防止过度调整。
====================================================================
"""
import logging
from dataclasses import dataclass
from typing import Dict

from config import (
    SIGNAL_WEIGHTS, DECISION_BUY_THRESHOLD, DECISION_SELL_THRESHOLD,
    PICKER_MIN_SCORE, TRADE_ADAPTIVE_MIN_SCORE_BASE,
    MAX_POSITIONS, MAX_SINGLE_PCT,
    STOP_LOSS, TAKE_PROFIT, TRAILING_STOP,
)

logger = logging.getLogger("strategy.regime_config")

TRADE_SCORE_MIN = 45
TRADE_SCORE_MAX = 75
TRADE_TOP_K_MIN = 1
TRADE_TOP_K_MAX = 5
TRADE_WEIGHT_MIN = 0.03
TRADE_WEIGHT_MAX = 0.25


@dataclass
class StrategyConfig:
    """策略配置"""
    # 信号权重
    signal_weights: Dict[str, float]
    # 决策阈值
    buy_threshold: float
    sell_threshold: float
    # 选股参数
    min_score: float
    # 仓位参数
    max_positions: int
    max_single_pct: float
    # 止损止盈
    stop_loss: float       # 负数，如 -0.08
    take_profit: float     # 正数，如 0.10
    trailing_stop: float   # 负数，如 -0.03
    # 环境标签
    regime: str
    label: str
    description: str

    def __repr__(self):
        return f"StrategyConfig({self.regime}/{self.label} buy_th={self.buy_threshold} max_pos={self.max_positions})"


# ══════════════════════════════════════════════════════════════════
# 各环境的策略配置
# ══════════════════════════════════════════════════════════════════

# 默认配置（基准）
_DEFAULT_CONFIG = StrategyConfig(
    signal_weights=dict(SIGNAL_WEIGHTS),
    buy_threshold=DECISION_BUY_THRESHOLD,
    sell_threshold=DECISION_SELL_THRESHOLD,
    min_score=PICKER_MIN_SCORE,
    max_positions=MAX_POSITIONS,
    max_single_pct=MAX_SINGLE_PCT,
    stop_loss=STOP_LOSS,
    take_profit=TAKE_PROFIT,
    trailing_stop=TRAILING_STOP,
    regime="default",
    label="默认",
    description="系统默认配置",
)

# 牛市配置 — 进攻型
_BULL_CONFIG = StrategyConfig(
    signal_weights={
        "technical": 0.35,    # 技术面略降（趋势明确时技术信号滞后）
        "capital": 0.15,      # 资金面升（资金推动行情）
        "sentiment": 0.20,    # 舆情面
        "emotion": 0.10,      # 情绪面降（牛市情绪高是正常的）
        "fundamental": 0.20,  # 基本面升（选好公司）
    },
    buy_threshold=55,         # 放宽买入门槛
    sell_threshold=30,        # 放宽卖出门槛（不轻易卖）
    min_score=50,             # 放宽选股门槛
    max_positions=5,          # 满仓运作
    max_single_pct=0.20,      # 单只上限不变
    stop_loss=-0.10,          # 放宽止损（牛市波动大）
    take_profit=0.15,         # 提高止盈目标
    trailing_stop=-0.05,      # 放宽移动止损
    regime="bull",
    label="牛市",
    description="进攻型配置: 放宽门槛, 提高止盈, 宽松止损",
)

# 熊市配置 — 防守型
_BEAR_CONFIG = StrategyConfig(
    signal_weights={
        "technical": 0.40,    # 技术面（趋势判断更重要）
        "capital": 0.10,      # 资金面
        "sentiment": 0.25,    # 舆情面（利空消息要重视）
        "emotion": 0.15,      # 情绪面（恐慌情绪参考）
        "fundamental": 0.10,  # 基本面
    },
    buy_threshold=70,         # 收紧买入门槛（只买强信号）
    sell_threshold=40,        # 降低卖出门槛（及时止损）
    min_score=60,             # 收紧选股门槛
    max_positions=3,          # 降低最大持仓（半仓以下）
    max_single_pct=0.15,      # 降低单只上限
    stop_loss=-0.06,          # 收紧止损（熊市要果断）
    take_profit=0.08,         # 降低止盈目标（快进快出）
    trailing_stop=-0.03,      # 收紧移动止损
    regime="bear",
    label="熊市",
    description="防守型配置: 收紧门槛, 降低仓位, 严格止损",
)

# 震荡配置 — 平衡型
_SIDEWAYS_CONFIG = StrategyConfig(
    signal_weights=dict(SIGNAL_WEIGHTS),  # 用默认权重
    buy_threshold=60,         # 中性门槛
    sell_threshold=35,        # 中性门槛
    min_score=55,             # 中性选股
    max_positions=4,          # 适度持仓
    max_single_pct=0.18,      # 中性仓位
    stop_loss=-0.08,          # 标准止损
    take_profit=0.10,         # 标准止盈
    trailing_stop=-0.03,      # 标准移动止损
    regime="sideways",
    label="震荡",
    description="平衡型配置: 中性参数, 注重止损纪律",
)

# 反弹配置 — 谨慎型
_REBOUND_CONFIG = StrategyConfig(
    signal_weights={
        "technical": 0.40,    # 技术面（反弹力度判断）
        "capital": 0.15,      # 资金面（抄底资金）
        "sentiment": 0.20,    # 舆情面
        "emotion": 0.15,      # 情绪面（恐慌后的修复）
        "fundamental": 0.10,  # 基本面
    },
    buy_threshold=60,         # 中性门槛
    sell_threshold=35,        # 中性门槛
    min_score=55,             # 中性选股
    max_positions=3,          # 谨慎持仓（反弹不确定）
    max_single_pct=0.15,      # 降低仓位
    stop_loss=-0.07,          # 偏紧止损（反弹可能二次下跌）
    take_profit=0.08,         # 快速止盈（落袋为安）
    trailing_stop=-0.03,      # 标准移动止损
    regime="rebound",
    label="反弹",
    description="谨慎型配置: 谨慎建仓, 快速止盈, 防二次下跌",
)


# 配置映射
_REGIME_CONFIGS = {
    "bull": _BULL_CONFIG,
    "bear": _BEAR_CONFIG,
    "sideways": _SIDEWAYS_CONFIG,
    "rebound": _REBOUND_CONFIG,
}


def get_config(regime: str = "sideways") -> StrategyConfig:
    """
    根据市场环境获取策略配置

    Args:
        regime: 市场环境 (bull/bear/sideways/rebound)

    Returns:
        StrategyConfig 对象
    """
    config = _REGIME_CONFIGS.get(regime, _SIDEWAYS_CONFIG)
    logger.info(f"策略配置: {config}")
    return config


def get_adaptive_config(regime: str = "sideways", adaptive_state: dict = None) -> StrategyConfig:
    """
    获取自适应调整后的策略配置

    先取环境基础配置，再叠加自适应引擎的调整。

    Args:
        regime: 市场环境
        adaptive_state: 自适应引擎的状态（来自AdaptiveEngine.state）

    Returns:
        StrategyConfig 对象
    """
    base = get_config(regime)

    if not adaptive_state:
        return base

    # 叠加自适应调整
    adjusted_weights = adaptive_state.get("current_weights")
    adjusted_buy_threshold = adaptive_state.get("current_buy_threshold")
    adjusted_min_score = adaptive_state.get("current_min_score")

    if adjusted_weights:
        base.signal_weights = adjusted_weights
    if adjusted_buy_threshold:
        base.buy_threshold = adjusted_buy_threshold
    if adjusted_min_score:
        base.min_score = adjusted_min_score

    return base


def format_config_report(config: StrategyConfig) -> str:
    """格式化策略配置报告"""
    lines = [
        "=" * 50,
        f"策略配置 [{config.label}]",
        "=" * 50,
        f"  环境: {config.regime} ({config.label})",
        f"  说明: {config.description}",
        "-" * 50,
        f"  买入阈值: {config.buy_threshold}",
        f"  卖出阈值: {config.sell_threshold}",
        f"  最低选股分: {config.min_score}",
        f"  最大持仓: {config.max_positions}只",
        f"  单只上限: {config.max_single_pct:.0%}",
        f"  止损: {config.stop_loss:.0%}",
        f"  止盈: {config.take_profit:.0%}",
        f"  移动止损: {config.trailing_stop:.0%}",
        "-" * 50,
        "  信号权重:",
    ]
    for k, v in config.signal_weights.items():
        label = {"technical": "技术面", "capital": "资金面", "sentiment": "舆情面",
                 "emotion": "情绪面", "fundamental": "基本面"}.get(k, k)
        lines.append(f"    {label}: {v:.0%}")

    lines.append("=" * 50)
    return "\n".join(lines)


def _load_adaptive_state() -> dict:
    """从SQLite读取自适应状态，失败时返回空"""
    try:
        from data.database import Database
        with Database() as db:
            return db.get_adaptive_state() or {}
    except Exception:
        return {}


def get_trade_params(regime: str = "sideways", adaptive_state: dict = None) -> dict:
    """
    获取交易参数（动态TopK）

    熊市不是不买，而是小仓位、高标准。

    如果存在自适应状态，则把最低分、候选数量和仓位缩放叠加到
    当前市场环境参数上。

    Returns:
        {"top_k": int, "min_score": float, "max_weight": float}
    """
    params = {
        "bull":     {"top_k": 5, "min_score": 55, "max_weight": 0.20},
        "sideways": {"top_k": 3, "min_score": 58, "max_weight": 0.12},
        "rebound":  {"top_k": 3, "min_score": 56, "max_weight": 0.10},
        "bear":     {"top_k": 1, "min_score": 62, "max_weight": 0.05},
    }
    result = dict(params.get(regime, params["sideways"]))
    if adaptive_state is None:
        adaptive_state = _load_adaptive_state()
    if adaptive_state:
        adaptive_min_score = adaptive_state.get("current_min_score")
        if adaptive_min_score is not None:
            adaptive_min_score = float(adaptive_min_score)
            if adaptive_min_score < TRADE_SCORE_MIN:
                adaptive_delta = 0
            else:
                adaptive_delta = adaptive_min_score - TRADE_ADAPTIVE_MIN_SCORE_BASE
            result["min_score"] = max(
                TRADE_SCORE_MIN,
                min(TRADE_SCORE_MAX, result["min_score"] + adaptive_delta),
            )

        top_k_delta = adaptive_state.get(
            "current_top_k_delta",
            adaptive_state.get("top_k_delta", 0),
        )
        result["top_k"] = max(
            TRADE_TOP_K_MIN,
            min(TRADE_TOP_K_MAX, int(result["top_k"] + top_k_delta)),
        )
        result["top_k_delta"] = top_k_delta

        position_scale = adaptive_state.get(
            "current_position_scale",
            adaptive_state.get("position_scale", 1.0),
        )
        result["max_weight"] = max(
            TRADE_WEIGHT_MIN,
            min(TRADE_WEIGHT_MAX, result["max_weight"] * position_scale),
        )
        result["position_scale"] = position_scale

    logger.info(f"交易参数: {regime} → Top{result['top_k']} 最低{result['min_score']} 仓位上限{result['max_weight']:.0%}")
    return result


# 测试
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    for regime in ["bull", "bear", "sideways", "rebound"]:
        config = get_config(regime)
        print(format_config_report(config))
        print()
