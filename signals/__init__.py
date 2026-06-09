"""
信号引擎模块 - 统一入口
====================================================================
5维信号源:
  1. 技术面(30%) - 均线/MACD/量价/Ichimoku/Wyckoff/蜡烛形态/Fibonacci
  2. 资金面(25%) - 北向资金/主力资金/融资融券
  3. 舆情面(20%) - LLM情感分析(微博+新闻)
  4. 情绪面(15%) - 涨跌停比/换手率异常/量能
  5. 基本面(10%) - PE/PB分位数

每个信号输出 0-100 分数 + 方向(买入/卖出/持有)

使用方法:
    from signals.technical import ma_cross_signal, macd_signal, all_technical_signals
    from signals.capital import capital_signal, northbound_signal
    from signals.sentiment import sentiment_signal
    from signals.emotion import emotion_signal, limit_ratio_signal
    from signals.fundamental import fundamental_signal, pe_percentile_signal
    from signals.composite import composite_signal, batch_composite, format_signal_report
====================================================================
"""
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
logger = logging.getLogger("signals")


class Direction(Enum):
    """信号方向"""
    BUY = "买入"
    SELL = "卖出"
    HOLD = "持有"


@dataclass
class SignalResult:
    """
    统一信号输出

    Attributes:
        name: 信号名称
        score: 0-100, 50为中性, >50偏多, <50偏空
        direction: 买入/卖出/持有
        confidence: 置信度 0-1
        detail: 信号详情
    """
    name: str
    score: int          # 0-100
    direction: Direction
    confidence: float   # 0-1
    detail: str = ""

    def __post_init__(self):
        self.score = max(0, min(100, self.score))
        self.confidence = max(0.0, min(1.0, self.confidence))

    def __str__(self):
        return f"[{self.name}] {self.direction.value} | 分数:{self.score} 置信度:{self.confidence:.0%} | {self.detail}"


def score_to_direction(score: int) -> Direction:
    """分数转方向: >=60买入, <=40卖出, 其余持有"""
    if score >= 60:
        return Direction.BUY
    elif score <= 40:
        return Direction.SELL
    return Direction.HOLD


# 快捷导入 - 基础技术信号
from signals.technical import ma_cross_signal, macd_signal, volume_price_signal
# 快捷导入 - 高级技术信号
from signals.technical import (
    ichimoku_signal,
    volume_profile_signal,
    market_structure_signal,
    wyckoff_signal,
    candlestick_signal,
    fibonacci_cluster_signal,
    all_technical_signals,
)
# 快捷导入 - 资金信号
from signals.capital import capital_signal, northbound_signal, main_capital_signal, margin_signal
# 快捷导入 - 舆情信号
from signals.sentiment import sentiment_signal, market_mood_signal
# 快捷导入 - 情绪信号
from signals.emotion import emotion_signal, limit_ratio_signal, turnover_signal, volume_energy_signal
# 快捷导入 - 基本面信号
from signals.fundamental import fundamental_signal, pe_percentile_signal, pb_percentile_signal
# 快捷导入 - 融合信号
from signals.composite import composite_signal, batch_composite, format_signal_report
