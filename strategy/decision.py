"""
决策引擎
综合5维信号（技术/资金/舆情/情绪/基本面）输出 BUY/SELL/HOLD 决策
"""
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from datetime import datetime

from config import (
    SIGNAL_WEIGHTS,
    DECISION_BUY_THRESHOLD,
    DECISION_SELL_THRESHOLD,
    DECISION_MIN_CONFIDENCE,
)


def _get_adaptive_params() -> dict:
    """从自适应引擎读取调整后的参数，失败则用默认值"""
    try:
        from strategy.adaptive import AdaptiveEngine
        engine = AdaptiveEngine()
        return engine.get_adjusted_params()
    except Exception:
        return {
            "signal_weights": SIGNAL_WEIGHTS,
            "buy_threshold": DECISION_BUY_THRESHOLD,
            "min_score": 55,
        }
from signals import Direction, SignalResult

logger = logging.getLogger("strategy.decision")


@dataclass
class DimensionScore:
    """单维度评分"""
    name: str           # 维度名: technical/capital/sentiment/emotion/fundamental
    score: float        # 0-100
    confidence: float   # 0-1
    detail: str = ""


@dataclass
class TradeDecision:
    """交易决策"""
    code: str
    name: str
    action: str                    # "BUY" / "SELL" / "HOLD"
    composite_score: float         # 综合分 0-100
    confidence: float              # 置信度 0-1
    dimensions: Dict[str, DimensionScore]  # 各维度详情
    reason: str = ""               # 决策理由
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def __repr__(self):
        return f"[{self.action}] {self.code} {self.name} score={self.composite_score:.1f} conf={self.confidence:.0%}"


def compute_dimension_scores(
    code: str,
    df=None,
    weibo_posts: list = None,
    news_list: list = None,
) -> Dict[str, DimensionScore]:
    """
    计算5维分数

    Args:
        code: 股票代码
        df: 日线DataFrame (用于技术面)
        weibo_posts: 微博帖子列表
        news_list: 新闻列表

    Returns:
        5个维度的DimensionScore字典
    """
    dims = {}

    # 1. 技术面 (使用已有的高级技术信号)
    if df is not None and len(df) > 30:
        from signals.technical import all_technical_signals
        tech_signals = all_technical_signals(df)
        if tech_signals:
            avg_score = sum(s.score for s in tech_signals) / len(tech_signals)
            avg_conf = sum(s.confidence for s in tech_signals) / len(tech_signals)
            details = [f"{s.name}={s.score}" for s in tech_signals]
            dims["technical"] = DimensionScore(
                name="technical", score=avg_score,
                confidence=avg_conf, detail=" | ".join(details),
            )
    if "technical" not in dims:
        dims["technical"] = DimensionScore("technical", 50, 0.0, "无技术数据")

    # 2. 资金面 (主力资金 + 融资融券)
    try:
        from signals.sentiment import sentiment_signal
        cap_score = _compute_capital_score(code)
        dims["capital"] = DimensionScore(
            name="capital", score=cap_score, confidence=0.5,
            detail=f"资金面分数={cap_score}",
        )
    except Exception as e:
        dims["capital"] = DimensionScore("capital", 50, 0.0, f"资金数据异常: {e}")

    # 3. 舆情面
    try:
        from signals.sentiment import sentiment_signal
        sent_result = sentiment_signal(code=code, weibo_posts=weibo_posts, news_list=news_list)
        dims["sentiment"] = DimensionScore(
            name="sentiment", score=sent_result.score,
            confidence=sent_result.confidence, detail=sent_result.detail,
        )
    except Exception as e:
        dims["sentiment"] = DimensionScore("sentiment", 50, 0.0, f"舆情数据异常: {e}")

    # 4. 情绪面 (涨跌停比 + 恐贪指标)
    try:
        from signals.sentiment import market_mood_signal
        mood = market_mood_signal()
        dims["emotion"] = DimensionScore(
            name="emotion", score=mood.score,
            confidence=mood.confidence, detail=mood.detail,
        )
    except Exception as e:
        dims["emotion"] = DimensionScore("emotion", 50, 0.0, f"情绪数据异常: {e}")

    # 5. 基本面 (PE/PB等)
    try:
        fund_score = _compute_fundamental_score(code, df)
        dims["fundamental"] = DimensionScore(
            name="fundamental", score=fund_score, confidence=0.4,
            detail=f"基本面分数={fund_score}",
        )
    except Exception as e:
        dims["fundamental"] = DimensionScore("fundamental", 50, 0.0, f"基本面异常: {e}")

    return dims

_CAPITAL_SCORE_CACHE = None
_CAPITAL_SCORE_CACHE_TIME = 0


def _compute_capital_score(code: str) -> float:
    """
    计算资金面分数
    使用东方财富融资融券API（个股级别）+ 北向资金持仓作为参考
    """
    import time
    global _CAPITAL_SCORE_CACHE, _CAPITAL_SCORE_CACHE_TIME

    # 缓存5分钟内有效
    if _CAPITAL_SCORE_CACHE is not None and time.time() - _CAPITAL_SCORE_CACHE_TIME < 300:
        return _CAPITAL_SCORE_CACHE

    import requests
    score = 50.0

    try:
        # 东方财富融资融券个股数据
        url = (
            f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
            f"reportName=RPTA_WEB_RZRQ_GGMX&columns=DATE,RZMRE,RZYE,RQYL,RQYE"
            f"&filter=(SCODE=%22{code}%22)&pageNumber=1&pageSize=5"
            f"&sortTypes=-1&sortColumns=DATE&source=WEB&client=WEB"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        }
        resp = requests.get(url, headers=headers, timeout=8)
        data = resp.json()

        if data.get("result") and data["result"].get("data"):
            items = data["result"]["data"]
            if len(items) >= 2:
                latest_buy = float(items[0].get("RZMRE", 0) or 0)  # 融资买入额
                latest_balance = float(items[0].get("RZYE", 0) or 0)  # 融资余额
                prev_balance = float(items[1].get("RZYE", 0) or 0)

                # 融资余额增长 → 市场看多
                if prev_balance > 0:
                    change = (latest_balance - prev_balance) / prev_balance * 100
                    score += min(20, max(-20, change * 5))

                # 融资买入额大 → 资金积极
                if latest_buy > 0 and latest_balance > 0:
                    buy_ratio = latest_buy / latest_balance * 100
                    if buy_ratio > 5:
                        score += 5
                    elif buy_ratio > 2:
                        score += 2
    except Exception as e:
        # API失败时保持默认50分
        pass

    score = max(0, min(100, score))
    _CAPITAL_SCORE_CACHE = score
    _CAPITAL_SCORE_CACHE_TIME = time.time()
    return score


def _compute_fundamental_score(code: str, df=None) -> float:
    """
    计算基本面分数 (PE)
    使用腾讯实时行情API（已集成，稳定可靠）
    """
    score = 50.0

    try:
        from data.realtime import get_realtime
        quotes = get_realtime([code])  # 【Phase1-Task4】统一传入list参数
        quote = quotes[0] if quotes else None
        if quote and quote.pe > 0:
            pe = quote.pe
            if pe < 15:
                score += 20  # 低估值
            elif pe < 25:
                score += 10  # 合理估值
            elif pe < 50:
                score += 0   # 中性
            elif pe < 100:
                score -= 10  # 偏高
            else:
                score -= 20  # 高估值
        elif quote and quote.pe < 0:
            score -= 15  # 亏损
    except Exception:
        pass

    return max(0, min(100, score))


def make_decision(
    code: str,
    name: str = "",
    df=None,
    weibo_posts: list = None,
    news_list: list = None,
    weights: Dict[str, float] = None,
    buy_threshold: float = None,
    sell_threshold: float = None,
) -> TradeDecision:
    """
    决策主函数

    综合5维信号，输出 BUY/SELL/HOLD 决策。

    Args:
        code: 股票代码
        name: 股票名称
        df: 日线数据 DataFrame
        weibo_posts: 微博帖子
        news_list: 新闻列表
        weights: 自定义维度权重
        buy_threshold: 买入阈值
        sell_threshold: 卖出阈值

    Returns:
        TradeDecision
    """
    adaptive = _get_adaptive_params()
    if weights is None:
        weights = adaptive.get("signal_weights", SIGNAL_WEIGHTS)
    if buy_threshold is None:
        buy_threshold = adaptive.get("buy_threshold", DECISION_BUY_THRESHOLD)
    if sell_threshold is None:
        sell_threshold = DECISION_SELL_THRESHOLD

    # 计算5维分数
    dimensions = compute_dimension_scores(code, df, weibo_posts, news_list)

    # 加权融合
    total_weight = sum(weights.get(k, 0) for k in dimensions)
    if total_weight == 0:
        total_weight = 1

    composite = 0.0
    weighted_conf = 0.0
    for dim_name, dim in dimensions.items():
        w = weights.get(dim_name, 0)
        composite += dim.score * w
        weighted_conf += dim.confidence * w

    composite /= total_weight
    confidence = weighted_conf / total_weight

    # 决策
    if composite >= buy_threshold and confidence >= DECISION_MIN_CONFIDENCE:
        action = "BUY"
    elif composite <= sell_threshold and confidence >= DECISION_MIN_CONFIDENCE:
        action = "SELL"
    else:
        action = "HOLD"

    # 理由
    reasons = []
    for dim_name, dim in dimensions.items():
        if dim.score >= 65:
            reasons.append(f"{_dim_label(dim_name)}偏多({dim.score:.0f})")
        elif dim.score <= 35:
            reasons.append(f"{_dim_label(dim_name)}偏空({dim.score:.0f})")
    if not reasons:
        reason = "信号中性"
    else:
        reason = "、".join(reasons)

    decision = TradeDecision(
        code=code,
        name=name or code,
        action=action,
        composite_score=round(composite, 1),
        confidence=round(confidence, 2),
        dimensions=dimensions,
        reason=reason,
    )

    logger.info(f"决策: {decision} | {reason}")
    return decision


def make_decision_with_cache(
    code: str,
    name: str = "",
    df=None,
    weights: Dict[str, float] = None,
    buy_threshold: float = None,
    sell_threshold: float = None,
    weibo_posts: list = None,
    mood_result=None,
    sentiment_result=None,
) -> TradeDecision:
    """
    使用预取缓存数据的决策函数（避免重复API调用）

    Args:
        code: 股票代码
        name: 股票名称
        df: 日线数据
        weights: 权重
        buy_threshold: 买入阈值
        sell_threshold: 卖出阈值
        weibo_posts: 预取的微博数据
        mood_result: 预取的市场情绪SignalResult
        sentiment_result: 预取的舆情SignalResult
    """
    adaptive = _get_adaptive_params()
    if weights is None:
        weights = adaptive.get("signal_weights", SIGNAL_WEIGHTS)
    if buy_threshold is None:
        buy_threshold = adaptive.get("buy_threshold", DECISION_BUY_THRESHOLD)
    if sell_threshold is None:
        sell_threshold = DECISION_SELL_THRESHOLD

    dims = {}

    # 1. 技术面(需要个股日线数据)
    if df is not None and len(df) > 30:
        from signals.technical import all_technical_signals
        tech_signals = all_technical_signals(df)
        if tech_signals:
            avg_score = sum(s.score for s in tech_signals) / len(tech_signals)
            avg_conf = sum(s.confidence for s in tech_signals) / len(tech_signals)
            details = [f"{s.name}={s.score}" for s in tech_signals]
            dims["technical"] = DimensionScore(
                name="technical", score=avg_score,
                confidence=avg_conf, detail=" | ".join(details),
            )
    if "technical" not in dims:
        dims["technical"] = DimensionScore("technical", 50, 0.0, "无技术数据")

    # 2. 资金面(复用缓存逻辑)
    try:
        cap_score = _compute_capital_score(code)
        dims["capital"] = DimensionScore(
            name="capital", score=cap_score, confidence=0.5,
            detail=f"资金面分数={cap_score}",
        )
    except Exception as e:
        dims["capital"] = DimensionScore("capital", 50, 0.0, f"资金数据异常: {e}")

    # 3. 舆情面(使用预取的批量结果)
    if sentiment_result:
        dims["sentiment"] = DimensionScore(
            name="sentiment", score=sentiment_result.score,
            confidence=sentiment_result.confidence, detail=sentiment_result.detail,
        )
    else:
        dims["sentiment"] = DimensionScore("sentiment", 50, 0.0, "无舆情数据")

    # 4. 情绪面(使用预取的全局情绪)
    if mood_result:
        dims["emotion"] = DimensionScore(
            name="emotion", score=mood_result.score,
            confidence=mood_result.confidence, detail=mood_result.detail,
        )
    else:
        dims["emotion"] = DimensionScore("emotion", 50, 0.0, "无情绪数据")

    # 5. 基本面
    try:
        fund_score = _compute_fundamental_score(code, df)
        dims["fundamental"] = DimensionScore(
            name="fundamental", score=fund_score, confidence=0.4,
            detail=f"基本面分数={fund_score}",
        )
    except Exception as e:
        dims["fundamental"] = DimensionScore("fundamental", 50, 0.0, f"基本面异常: {e}")

    # 加权融合
    total_weight = sum(weights.get(k, 0) for k in dims)
    if total_weight == 0:
        total_weight = 1

    composite = 0.0
    weighted_conf = 0.0
    for dim_name, dim in dims.items():
        w = weights.get(dim_name, 0)
        composite += dim.score * w
        weighted_conf += dim.confidence * w

    composite /= total_weight
    confidence = weighted_conf / total_weight

    if composite >= buy_threshold and confidence >= DECISION_MIN_CONFIDENCE:
        action = "BUY"
    elif composite <= sell_threshold and confidence >= DECISION_MIN_CONFIDENCE:
        action = "SELL"
    else:
        action = "HOLD"

    reasons = []
    for dim_name, dim in dims.items():
        if dim.score >= 65:
            reasons.append(f"{_dim_label(dim_name)}偏多({dim.score:.0f})")
        elif dim.score <= 35:
            reasons.append(f"{_dim_label(dim_name)}偏空({dim.score:.0f})")
    reason = "、".join(reasons) if reasons else "信号中性"

    decision = TradeDecision(
        code=code, name=name or code, action=action,
        composite_score=round(composite, 1), confidence=round(confidence, 2),
        dimensions=dims, reason=reason,
    )

    logger.info(f"决策: {decision} | {reason}")
    return decision


def batch_decide(
    candidates: list,
    get_df_func=None,
    weights: Dict[str, float] = None,
) -> List[TradeDecision]:
    """
    批量决策（优化版）

    优化点:
    1. 预取共享数据(微博/情绪/涨停板)避免重复获取
    2. 批量LLM舆情分析(一次调用处理所有股票)
    3. 按需获取个股数据

    Args:
        candidates: 候选列表 (Candidate对象 或 dict)
        get_df_func: 获取日线数据的函数 code -> DataFrame
        weights: 自定义权重

    Returns:
        按分数降序排列的决策列表
    """
    import time

    # ── 预取共享数据(只获取一次) ──
    logger.info("预取共享数据...")
    t0 = time.time()

    # 微博数据
    weibo_posts = []
    try:
        from data.sentiment import get_weibo_finance
        weibo_posts = get_weibo_finance(10)
        logger.info(f"微博数据: {len(weibo_posts)}条 ({time.time()-t0:.1f}s)")
    except Exception as e:
        logger.warning(f"微博获取失败: {e}")

    # 市场情绪(涨跌停比) - 全局共享
    mood_result = None
    try:
        from signals.sentiment import market_mood_signal
        mood_result = market_mood_signal()
        logger.info(f"市场情绪: {mood_result.score} ({time.time()-t0:.1f}s)")
    except Exception as e:
        logger.warning(f"情绪获取失败: {e}")

    # ── 批量LLM舆情分析(一次调用) ──
    stock_list = []
    for c in candidates:
        code = c.code if hasattr(c, "code") else c.get("code", "")
        name = c.name if hasattr(c, "name") else c.get("name", "")
        stock_list.append({"code": code, "name": name})

    batch_sentiment_scores = {}
    try:
        from signals.sentiment import batch_sentiment_signal
        batch_sentiment_scores = batch_sentiment_signal(stock_list, weibo_posts)
        logger.info(f"批量舆情分析完成: {len(batch_sentiment_scores)}只 ({time.time()-t0:.1f}s)")
    except Exception as e:
        logger.warning(f"批量舆情分析失败: {e}")

    # ── 逐只股票决策(使用预取的数据) ──
    decisions = []
    for c in candidates:
        code = c.code if hasattr(c, "code") else c.get("code", "")
        name = c.name if hasattr(c, "name") else c.get("name", "")

        df = None
        if get_df_func:
            try:
                df = get_df_func(code)
            except Exception:
                pass

        try:
            decision = make_decision_with_cache(
                code, name, df,
                weights=weights,
                weibo_posts=weibo_posts,
                mood_result=mood_result,
                sentiment_result=batch_sentiment_scores.get(code),
            )
            decisions.append(decision)
        except Exception as e:
            logger.error(f"决策失败 {code}: {e}")

    decisions.sort(key=lambda x: x.composite_score, reverse=True)
    return decisions


def _dim_label(dim_name: str) -> str:
    """维度中文标签"""
    return {
        "technical": "技术面",
        "capital": "资金面",
        "sentiment": "舆情面",
        "emotion": "情绪面",
        "fundamental": "基本面",
    }.get(dim_name, dim_name)


def format_decision_report(decisions: List[TradeDecision]) -> str:
    """格式化决策报告"""
    if not decisions:
        return "无决策结果"

    lines = [
        "=" * 70,
        "决策报告",
        "=" * 70,
    ]

    for d in decisions:
        action_emoji = {"BUY": "[买入]", "SELL": "[卖出]", "HOLD": "[持有]"}.get(d.action, "[?]")
        lines.append(f"\n{action_emoji} {d.code} {d.name}")
        lines.append(f"  综合分: {d.composite_score:.1f} | 置信度: {d.confidence:.0%} | 决策: {d.action}")
        for dim_name in ["technical", "capital", "sentiment", "emotion", "fundamental"]:
            dim = d.dimensions.get(dim_name)
            if dim:
                lines.append(f"    {_dim_label(dim_name)}: {dim.score:.1f} (conf={dim.confidence:.0%}) {dim.detail}")
        lines.append(f"  理由: {d.reason}")

    lines.append("\n" + "=" * 70)
    buy_count = sum(1 for d in decisions if d.action == "BUY")
    sell_count = sum(1 for d in decisions if d.action == "SELL")
    hold_count = sum(1 for d in decisions if d.action == "HOLD")
    lines.append(f"汇总: 买入{buy_count} 卖出{sell_count} 持有{hold_count}")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    # 测试单只股票决策
    from data.history import get_daily
    code = "600519"
    df = get_daily(code, start_date="20240101")
    decision = make_decision(code, "贵州茅台", df)
    print(format_decision_report([decision]))
