"""
多信号融合模块 - 5维融合
====================================================================
将5个维度的信号加权融合，输出最终交易建议:
  技术面 30% + 资金面 25% + 舆情面 20% + 情绪面 15% + 基本面 10%

使用方法:
    from signals.composite import composite_signal, batch_composite, format_signal_report
====================================================================
"""
from dataclasses import dataclass
from typing import List, Optional, Dict
from signals import SignalResult, Direction, score_to_direction, logger
from config import SIGNAL_WEIGHTS


@dataclass
class CompositeSignal:
    """融合信号结果"""
    code: str               # 股票代码
    name: str               # 股票名称
    final_score: float      # 最终分数 0-100
    direction: Direction    # 买入/卖出/持有
    confidence: float       # 置信度 0-1
    signals: dict           # 各维度分数
    reason: str             # 综合理由


def composite_signal(
    code: str,
    name: str = "",
    technical_score: float = 50,
    capital_score: float = 50,
    sentiment_score: float = 50,
    emotion_score: float = 50,
    fundamental_score: float = 50,
    weights: dict = None,
) -> CompositeSignal:
    """
    5维信号融合

    Args:
        code: 股票代码
        name: 股票名称
        technical_score: 技术信号分数 (0-100)
        capital_score: 资金信号分数 (0-100)
        sentiment_score: 舆情信号分数 (0-100)
        emotion_score: 情绪信号分数 (0-100)
        fundamental_score: 基本面信号分数 (0-100)
        weights: 自定义权重, 默认从config.SIGNAL_WEIGHTS读取

    Returns:
        CompositeSignal: 融合后的信号
    """
    if weights is None:
        weights = SIGNAL_WEIGHTS

    # 加权计算
    score_map = {
        "technical": technical_score,
        "capital": capital_score,
        "sentiment": sentiment_score,
        "emotion": emotion_score,
        "fundamental": fundamental_score,
    }

    total_weight = sum(weights.get(k, 0) for k in score_map)
    if total_weight == 0:
        total_weight = 1

    final_score = sum(
        score_map[k] * weights.get(k, 0) for k in score_map
    ) / total_weight

    # 置信度: 各信号一致性越高, 置信度越高
    scores = list(score_map.values())
    avg = sum(scores) / len(scores)
    variance = sum((s - avg) ** 2 for s in scores) / len(scores)
    confidence = max(0, min(1, 1 - variance / 2500))

    direction = score_to_direction(final_score)

    # 生成理由
    label_map = {
        "technical": "技术面",
        "capital": "资金面",
        "sentiment": "舆情面",
        "emotion": "情绪面",
        "fundamental": "基本面",
    }
    reasons = []
    for k, label in label_map.items():
        s = score_map[k]
        if s >= 60:
            reasons.append(f"{label}偏多({s:.0f})")
        elif s <= 40:
            reasons.append(f"{label}偏空({s:.0f})")

    if not reasons:
        reason = "信号中性，建议观望"
    else:
        reason = "、".join(reasons)

    return CompositeSignal(
        code=code,
        name=name or code,
        final_score=round(final_score, 1),
        direction=direction,
        confidence=round(confidence, 2),
        signals=score_map,
        reason=reason,
    )


def composite_from_signals(
    code: str,
    name: str = "",
    technical_result: SignalResult = None,
    capital_result: SignalResult = None,
    sentiment_result: SignalResult = None,
    emotion_result: SignalResult = None,
    fundamental_result: SignalResult = None,
    weights: dict = None,
) -> CompositeSignal:
    """
    从SignalResult对象直接融合

    Args:
        code: 股票代码
        name: 股票名称
        *_result: 各维度SignalResult对象

    Returns:
        CompositeSignal: 融合后的信号
    """
    return composite_signal(
        code=code,
        name=name,
        technical_score=technical_result.score if technical_result else 50,
        capital_score=capital_result.score if capital_result else 50,
        sentiment_score=sentiment_result.score if sentiment_result else 50,
        emotion_score=emotion_result.score if emotion_result else 50,
        fundamental_score=fundamental_result.score if fundamental_result else 50,
        weights=weights,
    )


def batch_composite(
    stocks: list,
    technical_scores: dict = None,
    capital_scores: dict = None,
    sentiment_scores: dict = None,
    emotion_scores: dict = None,
    fundamental_scores: dict = None,
    weights: dict = None,
    min_score: float = 60,
) -> List[CompositeSignal]:
    """
    批量5维信号融合, 返回符合条件的股票

    Args:
        stocks: 股票列表 [{"code": "600519", "name": "贵州茅台"}, ...]
        *_scores: {"600519": 75, ...} 各维度分数字典
        weights: 自定义权重
        min_score: 最低分数阈值

    Returns:
        按分数降序排列的信号列表
    """
    if technical_scores is None:
        technical_scores = {}
    if capital_scores is None:
        capital_scores = {}
    if sentiment_scores is None:
        sentiment_scores = {}
    if emotion_scores is None:
        emotion_scores = {}
    if fundamental_scores is None:
        fundamental_scores = {}

    results = []
    for stock in stocks:
        code = stock.get("code", "")
        name = stock.get("name", "")

        signal = composite_signal(
            code=code,
            name=name,
            technical_score=technical_scores.get(code, 50),
            capital_score=capital_scores.get(code, 50),
            sentiment_score=sentiment_scores.get(code, 50),
            emotion_score=emotion_scores.get(code, 50),
            fundamental_score=fundamental_scores.get(code, 50),
            weights=weights,
        )

        if signal.final_score >= min_score:
            results.append(signal)

    results.sort(key=lambda x: x.final_score, reverse=True)
    return results


def format_signal_report(signals: List[CompositeSignal]) -> str:
    """格式化5维信号报告"""
    if not signals:
        return "无符合条件的信号"

    lines = ["=" * 70]
    lines.append("量化信号报告(5维融合)")
    lines.append("=" * 70)
    lines.append(f"权重: 技术{SIGNAL_WEIGHTS['technical']:.0%} "
                 f"资金{SIGNAL_WEIGHTS['capital']:.0%} "
                 f"舆情{SIGNAL_WEIGHTS['sentiment']:.0%} "
                 f"情绪{SIGNAL_WEIGHTS['emotion']:.0%} "
                 f"基本面{SIGNAL_WEIGHTS['fundamental']:.0%}")
    lines.append("-" * 70)

    for s in signals:
        emoji = {"买入": "[BUY]", "卖出": "[SELL]", "持有": "[HOLD]"}.get(s.direction.value, "[?]")
        lines.append(f"\n{emoji} {s.code} {s.name}")
        lines.append(f"  综合分数: {s.final_score} | 方向: {s.direction.value} | 置信度: {s.confidence:.0%}")
        lines.append(f"  技术:{s.signals['technical']:.0f} "
                     f"资金:{s.signals['capital']:.0f} "
                     f"舆情:{s.signals['sentiment']:.0f} "
                     f"情绪:{s.signals['emotion']:.0f} "
                     f"基本面:{s.signals['fundamental']:.0f}")
        lines.append(f"  理由: {s.reason}")

    lines.append("\n" + "=" * 70)
    lines.append(f"共 {len(signals)} 只股票符合买入信号")
    return "\n".join(lines)


# 测试
if __name__ == "__main__":
    stocks = [
        {"code": "600519", "name": "贵州茅台"},
        {"code": "300750", "name": "宁德时代"},
        {"code": "000001", "name": "平安银行"},
    ]

    technical = {"600519": 75, "300750": 65, "000001": 45}
    capital = {"600519": 70, "300750": 60, "000001": 50}
    sentiment = {"600519": 80, "300750": 55, "000001": 60}
    emotion = {"600519": 65, "300750": 70, "000001": 40}
    fundamental = {"600519": 55, "300750": 45, "000001": 70}

    signals = batch_composite(stocks, technical, capital, sentiment, emotion, fundamental)
    print(format_signal_report(signals))
