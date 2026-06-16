"""
市场环境识别器
====================================================================
综合5维择时信号 + 沪深300趋势 + 市场广度，识别当前市场环境:
  - bull     (牛市): 持续上涨，情绪高涨
  - bear     (熊市): 持续下跌，情绪低迷
  - sideways (震荡): 区间波动，方向不明
  - rebound  (反弹): 急跌后回升

结果存入 SQLite market_regimes 表，供策略配置和LLM决策参考。
====================================================================
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("strategy.market_regime")


@dataclass
class MarketRegime:
    """市场环境"""
    date: str
    regime: str          # bull / bear / sideways / rebound
    confidence: float    # 0-1
    indicators: Dict     # 原始指标数据
    llm_reasoning: str = ""  # LLM推理过程（可选）

    @property
    def label(self) -> str:
        return {
            "bull": "牛市",
            "bear": "熊市",
            "sideways": "震荡",
            "rebound": "反弹",
        }.get(self.regime, "未知")

    def __repr__(self):
        return f"MarketRegime({self.regime}/{self.label} conf={self.confidence:.0%})"


def _calc_trend_indicators() -> Dict:
    """
    计算趋势类指标

    Returns:
        {
            "hs300_pct_5d": float,   # 沪深300近5日涨跌幅
            "hs300_pct_20d": float,  # 沪深300近20日涨跌幅
            "hs300_above_ma20": bool, # 价格在20日均线上方
            "hs300_above_ma60": bool, # 价格在60日均线上方
            "ma20_slope": float,     # 20日均线斜率(正=上升)
        }
    """
    indicators = {}
    try:
        import pandas as pd
        from data.history import get_daily

        df = get_daily("000300.SH", start_date="20240101")
        if df is None or len(df) < 60:
            return indicators

        close = df["close"].astype(float)
        cur = close.iloc[-1]

        # 近5日涨跌幅
        if len(close) >= 6:
            indicators["hs300_pct_5d"] = (cur / close.iloc[-6] - 1) * 100

        # 近20日涨跌幅
        if len(close) >= 21:
            indicators["hs300_pct_20d"] = (cur / close.iloc[-21] - 1) * 100

        # 均线
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()

        indicators["hs300_above_ma20"] = cur > ma20.iloc[-1] if not pd.isna(ma20.iloc[-1]) else None
        indicators["hs300_above_ma60"] = cur > ma60.iloc[-1] if not pd.isna(ma60.iloc[-1]) else None

        # 均线斜率（近5日均线变化率）
        if not pd.isna(ma20.iloc[-1]) and not pd.isna(ma20.iloc[-6]):
            indicators["ma20_slope"] = (ma20.iloc[-1] / ma20.iloc[-6] - 1) * 100

    except Exception as e:
        logger.warning(f"趋势指标计算失败: {e}")

    return indicators


def _calc_breadth_indicators() -> Dict:
    """
    计算市场广度指标

    Returns:
        {
            "limit_up_count": int,    # 涨停数
            "limit_down_count": int,  # 跌停数
            "limit_ratio": float,     # 涨跌停比
        }
    """
    indicators = {}
    try:
        from data.market import get_limit_up, get_limit_down
        df_up = get_limit_up()
        df_down = get_limit_down()

        up_count = len(df_up) if df_up is not None else 0
        down_count = len(df_down) if df_down is not None else 0

        indicators["limit_up_count"] = up_count
        indicators["limit_down_count"] = down_count

        total = up_count + down_count
        indicators["limit_ratio"] = up_count / total if total > 0 else 0.5

    except Exception as e:
        logger.warning(f"市场广度指标计算失败: {e}")

    return indicators


def _get_timing_signals() -> Dict:
    """
    获取五维择时信号

    Returns:
        {
            "final_signal": int,     # -1/0/+1
            "position_pct": int,     # 0/50/100
            "vote_detail": str,
            "signals": dict,         # 各维度信号
        }
    """
    try:
        from signals.market_timing import compute_market_timing
        result = compute_market_timing()
        return {
            "final_signal": result.final_signal,
            "position_pct": result.position_pct,
            "vote_detail": result.vote_detail,
            "signals": {
                "valuation": result.valuation.signal,
                "capital": result.capital.signal,
                "technical": result.technical.signal,
                "emotion": result.emotion.signal,
                "fundamental": result.fundamental.signal,
            },
        }
    except Exception as e:
        logger.warning(f"择时信号获取失败: {e}")
        return {"final_signal": 0, "position_pct": 50, "vote_detail": f"异常: {e}", "signals": {}}


def _classify_regime(indicators: Dict) -> tuple:
    """
    基于规则的市场环境分类

    Returns:
        (regime: str, confidence: float, reasoning: str)
    """
    scores = {"bull": 0, "bear": 0, "sideways": 0, "rebound": 0}
    reasons = []

    # 1. 五维择时信号权重最大
    timing = indicators.get("timing_final_signal", 0)
    if timing == 1:
        scores["bull"] += 3
        reasons.append("五维择时看多")
    elif timing == -1:
        scores["bear"] += 3
        reasons.append("五维择时看空")
    else:
        scores["sideways"] += 2
        reasons.append("五维择时中性")

    # 2. 沪深300趋势
    pct_5d = indicators.get("hs300_pct_5d", 0)
    pct_20d = indicators.get("hs300_pct_20d", 0)

    if pct_5d and pct_5d > 3:
        scores["bull"] += 2
        reasons.append(f"近5日涨{pct_5d:.1f}%")
    elif pct_5d and pct_5d < -3:
        scores["bear"] += 2
        reasons.append(f"近5日跌{pct_5d:.1f}%")
        # 急跌后可能反弹
        if pct_20d and pct_20d < -8:
            scores["rebound"] += 2
            reasons.append("20日跌幅大，可能反弹")

    if pct_20d and pct_20d > 8:
        scores["bull"] += 2
        reasons.append(f"近20日涨{pct_20d:.1f}%")
    elif pct_20d and pct_20d < -8:
        scores["bear"] += 2
        reasons.append(f"近20日跌{pct_20d:.1f}%")

    # 3. 均线位置
    above_ma20 = indicators.get("hs300_above_ma20")
    above_ma60 = indicators.get("hs300_above_ma60")
    if above_ma20 is True:
        scores["bull"] += 1
        reasons.append("在MA20上方")
    elif above_ma20 is False:
        scores["bear"] += 1
        reasons.append("在MA20下方")

    if above_ma60 is True:
        scores["bull"] += 1
    elif above_ma60 is False:
        scores["bear"] += 1

    # 4. 均线斜率
    slope = indicators.get("ma20_slope", 0)
    if slope and slope > 1:
        scores["bull"] += 1
        reasons.append("MA20上升")
    elif slope and slope < -1:
        scores["bear"] += 1
        reasons.append("MA20下降")

    # 5. 涨跌停比
    limit_ratio = indicators.get("limit_ratio", 0.5)
    if limit_ratio > 0.7:
        scores["bull"] += 1
        reasons.append(f"涨停占比{limit_ratio:.0%}")
    elif limit_ratio < 0.3:
        scores["bear"] += 1
        reasons.append(f"涨停占比{limit_ratio:.0%}")

    # 选出最高分的环境
    best = max(scores, key=scores.get)
    total = sum(scores.values())
    confidence = scores[best] / total if total > 0 else 0.5

    # 如果最高分和第二高分差距不大，偏向sideways
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    if len(sorted_scores) >= 2:
        top1, top2 = sorted_scores[0], sorted_scores[1]
        if top1[1] - top2[1] <= 1 and top1[0] != "sideways":
            best = "sideways"
            confidence = 0.5
            reasons.append("多空力量接近，判定震荡")

    # 反弹特殊判断：之前是bear但现在timing转中性/看多
    if best == "bear" and timing >= 0 and pct_5d and pct_5d > 0:
        best = "rebound"
        reasons.append("从熊市中反弹")

    return best, round(confidence, 2), " | ".join(reasons)


def _classify_regime_llm(indicators: Dict) -> Optional[tuple]:
    """
    用LLM辅助判断市场环境（可选，失败则fallback到规则判断）

    Returns:
        (regime, confidence, reasoning) 或 None
    """
    import os
    import requests

    api_key = os.environ.get("XIAOMI_API_KEY", "")
    base_url = "https://token-plan-cn.xiaomimimo.com/v1"
    if not api_key:
        return None

    prompt = f"""你是A股市场环境分析师。根据以下指标判断当前市场环境。

指标数据:
- 五维择时信号: {indicators.get('timing_final_signal', 0)} (-1看空/0中性/+1看多)
- 建议仓位: {indicators.get('timing_position_pct', 50)}%
- 沪深300近5日涨跌: {indicators.get('hs300_pct_5d', 'N/A')}%
- 沪深300近20日涨跌: {indicators.get('hs300_pct_20d', 'N/A')}%
- 价格在MA20上方: {indicators.get('hs300_above_ma20', 'N/A')}
- 价格在MA60上方: {indicators.get('hs300_above_ma60', 'N/A')}
- MA20斜率: {indicators.get('ma20_slope', 'N/A')}%
- 涨停数: {indicators.get('limit_up_count', 'N/A')}
- 跌停数: {indicators.get('limit_down_count', 'N/A')}
- 涨跌停比: {indicators.get('limit_ratio', 'N/A')}

请返回JSON:
{{"regime": "bull/bear/sideways/rebound", "confidence": 0.0-1.0, "reasoning": "一句话判断理由"}}

只返回JSON，不要其他文字。"""

    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "mimo-v2.5-pro",
            "messages": [
                {"role": "system", "content": "你是A股市场环境分析师，用中文回答。"},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 1000,
            "temperature": 0.1,
        }
        resp = requests.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        content = (msg.get("content", "") or "").strip()
        # MiMo模型: max_tokens不够时内容可能只在reasoning_content里
        if not content:
            content = (msg.get("reasoning_content", "") or "").strip()

        # 解析JSON
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        data = json.loads(content)
        regime = data.get("regime", "sideways")
        if regime not in ("bull", "bear", "sideways", "rebound"):
            regime = "sideways"
        confidence = float(data.get("confidence", 0.5))
        reasoning = data.get("reasoning", "")
        return regime, confidence, reasoning

    except Exception as e:
        logger.debug(f"LLM市场环境判断失败: {e}")
        return None


def detect_regime(use_llm: bool = True) -> MarketRegime:
    """
    主函数: 检测当前市场环境

    流程:
    1. 收集所有指标
    2. 尝试LLM判断（可选）
    3. LLM失败时用规则判断
    4. 结果存入SQLite

    Args:
        use_llm: 是否使用LLM辅助判断

    Returns:
        MarketRegime 对象
    """
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"市场环境检测开始 {today}")

    # 1. 收集指标
    indicators = {}

    # 趋势指标
    trend = _calc_trend_indicators()
    indicators.update(trend)

    # 市场广度
    breadth = _calc_breadth_indicators()
    indicators.update(breadth)

    # 五维择时
    timing = _get_timing_signals()
    indicators["timing_final_signal"] = timing["final_signal"]
    indicators["timing_position_pct"] = timing["position_pct"]
    indicators["timing_vote_detail"] = timing["vote_detail"]
    indicators["timing_signals"] = timing["signals"]

    # 2. 尝试LLM判断
    llm_reasoning = ""
    if use_llm:
        llm_result = _classify_regime_llm(indicators)
        if llm_result:
            regime, confidence, llm_reasoning = llm_result
            logger.info(f"LLM市场环境: {regime} conf={confidence:.0%}")
        else:
            regime, confidence, reasoning = _classify_regime(indicators)
            llm_reasoning = f"规则判断: {reasoning}"
    else:
        regime, confidence, reasoning = _classify_regime(indicators)
        llm_reasoning = f"规则判断: {reasoning}"

    # 3. 构建结果
    result = MarketRegime(
        date=today,
        regime=regime,
        confidence=confidence,
        indicators=indicators,
        llm_reasoning=llm_reasoning,
    )

    # 4. 存入SQLite
    try:
        from data.database import Database
        with Database() as db:
            db.insert_market_regime({
                "date": today,
                "regime": regime,
                "confidence": confidence,
                "indicators": json.dumps(indicators, ensure_ascii=False, default=str),
                "llm_reasoning": llm_reasoning,
            })
        logger.info(f"市场环境已存入SQLite: {result}")
    except Exception as e:
        logger.warning(f"市场环境存入SQLite失败: {e}")

    return result


def get_regime_history(days: int = 10) -> List[MarketRegime]:
    """
    获取最近N天的市场环境历史

    Args:
        days: 回看天数

    Returns:
        MarketRegime列表（按日期降序）
    """
    try:
        from data.database import Database
        with Database() as db:
            from datetime import timedelta
            start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            end = datetime.now().strftime("%Y-%m-%d")

            regimes = []
            c = db.conn.cursor()
            c.execute("""
                SELECT * FROM market_regimes
                WHERE date >= ? AND date <= ?
                ORDER BY date DESC
            """, (start, end))

            for row in c.fetchall():
                row_dict = dict(row)
                indicators = {}
                try:
                    indicators = json.loads(row_dict.get("indicators", "{}"))
                except Exception:
                    pass
                regimes.append(MarketRegime(
                    date=row_dict["date"],
                    regime=row_dict.get("regime", "sideways"),
                    confidence=row_dict.get("confidence", 0.5),
                    indicators=indicators,
                    llm_reasoning=row_dict.get("llm_reasoning", ""),
                ))
            return regimes

    except Exception as e:
        logger.warning(f"获取市场环境历史失败: {e}")
        return []


def format_regime_report(regime: MarketRegime) -> str:
    """格式化市场环境报告"""
    emoji = {"bull": "🟢", "bear": "🔴", "sideways": "🟡", "rebound": "🔵"}.get(regime.regime, "⚪")

    lines = [
        "=" * 50,
        f"市场环境识别 {regime.date}",
        "=" * 50,
        f"  环境: {emoji} {regime.label} ({regime.regime})",
        f"  置信度: {regime.confidence:.0%}",
    ]

    # 关键指标
    ind = regime.indicators
    if ind:
        lines.append("-" * 50)
        if "timing_vote_detail" in ind:
            lines.append(f"  择时: {ind['timing_vote_detail']}")
        if "hs300_pct_5d" in ind:
            lines.append(f"  沪深300 5日: {ind['hs300_pct_5d']:+.1f}%")
        if "hs300_pct_20d" in ind:
            lines.append(f"  沪深300 20日: {ind['hs300_pct_20d']:+.1f}%")
        if "limit_up_count" in ind:
            lines.append(f"  涨停: {ind.get('limit_up_count', 0)}只  跌停: {ind.get('limit_down_count', 0)}只")

    if regime.llm_reasoning:
        lines.append("-" * 50)
        lines.append(f"  判断依据: {regime.llm_reasoning}")

    lines.append("=" * 50)
    return "\n".join(lines)


# 测试
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    regime = detect_regime(use_llm=False)
    print(format_regime_report(regime))
