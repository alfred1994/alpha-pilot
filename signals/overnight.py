"""
隔夜风险信号模块
====================================================================
从隔夜快照读取外围市场+新闻数据，计算隔夜风险分数(0-100)。
分数越高表示外围环境越好（利好A股），越低表示风险越大。
====================================================================
"""
import json
import os
import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

logger = logging.getLogger("signals.overnight")

SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "snapshots")

@dataclass
class OvernightSignal:
    """隔夜风险信号"""
    score: float        # 0-100, 越高越好
    risk_level: str     # low/medium/high
    detail: str         # 一句话描述
    us_summary: str     # 美股概况
    a50_summary: str    # A50概况
    news_summary: str   # 新闻概况


def compute_overnight_signal(date: str = None) -> OvernightSignal:
    """
    计算隔夜风险信号

    Args:
        date: 日期 YYYY-MM-DD，默认今天

    Returns:
        OvernightSignal
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    # 读取快照
    snap_path = os.path.join(SNAPSHOT_DIR, f"overnight_{date}.json")
    if not os.path.exists(snap_path):
        logger.warning(f"隔夜快照不存在: {snap_path}，尝试生成")
        try:
            from data.overnight_snapshot import generate_overnight_snapshot
            snap = generate_overnight_snapshot()
        except Exception as e:
            logger.error(f"生成隔夜快照失败: {e}")
            return OvernightSignal(50, "medium", "无隔夜数据", "", "", "")
    else:
        with open(snap_path, "r", encoding="utf-8") as f:
            snap = json.load(f)

    # 基础分
    score = 60.0
    factors = []

    # 美股评分 (权重最高)
    us = snap.get("us_market", {}).get("us_indices", {})
    us_changes = []
    for key in ["djia", "nasdaq", "sp500"]:
        idx = us.get(key, {})
        pct = idx.get("change_pct", 0)
        if pct is not None:
            us_changes.append(pct)

    if us_changes:
        avg_us = sum(us_changes) / len(us_changes)
        if avg_us > 1.0:
            score += 15
            factors.append(f"美股大涨({avg_us:+.1f}%)")
        elif avg_us > 0.5:
            score += 10
            factors.append(f"美股上涨({avg_us:+.1f}%)")
        elif avg_us > 0:
            score += 5
            factors.append(f"美股微涨({avg_us:+.1f}%)")
        elif avg_us > -0.5:
            score -= 5
            factors.append(f"美股微跌({avg_us:+.1f}%)")
        elif avg_us > -1.0:
            score -= 10
            factors.append(f"美股下跌({avg_us:+.1f}%)")
        else:
            score -= 15
            factors.append(f"美股大跌({avg_us:+.1f}%)")

    us_summary = ", ".join([f"{k} {v.get('change_pct', 0):+.1f}%" for k, v in us.items() if v.get("change_pct") is not None])

    # A50期货评分
    a50 = snap.get("us_market", {}).get("a50_futures", {})
    a50_pct = a50.get("change_pct", 0)
    a50_summary = f"A50 {a50.get('price', 0)} ({a50_pct:+.1f}%)" if a50 else "A50 无数据"
    if a50_pct is not None:
        if a50_pct > 1.0:
            score += 10
            factors.append(f"A50期货上涨({a50_pct:+.1f}%)")
        elif a50_pct > 0.5:
            score += 5
            factors.append(f"A50期货微涨({a50_pct:+.1f}%)")
        elif a50_pct > -0.5:
            pass  # 中性
        elif a50_pct > -1.0:
            score -= 5
            factors.append(f"A50期货微跌({a50_pct:+.1f}%)")
        else:
            score -= 10
            factors.append(f"A50期货下跌({a50_pct:+.1f}%)")

    # 恒生指数评分 (权重较低)
    hk = snap.get("us_market", {}).get("hk_indices", {})
    hsi = hk.get("hsi", {})
    hsi_pct = hsi.get("change_pct", 0)
    if hsi_pct is not None:
        if hsi_pct > 1.0:
            score += 7
            factors.append(f"恒生上涨({hsi_pct:+.1f}%)")
        elif hsi_pct > 0.5:
            score += 4
        elif hsi_pct > -0.5:
            pass
        elif hsi_pct > -1.0:
            score -= 4
        else:
            score -= 7
            factors.append(f"恒生下跌({hsi_pct:+.1f}%)")

    # 风险等级调整
    snap_risk = snap.get("risk_assessment", {}).get("level", "medium")
    risk_adj = {"low": 0, "medium": -5, "high": -15}.get(snap_risk, -5)
    score += risk_adj
    if snap_risk == "high":
        factors.append("隔夜风险等级:高")

    # clamp
    score = max(0, min(100, score))

    # 确定风险等级
    if score >= 70:
        risk_level = "low"
    elif score >= 45:
        risk_level = "medium"
    else:
        risk_level = "high"

    # 新闻概况
    news_items = snap.get("news", {}).get("headlines", [])
    news_summary = "; ".join([n.get("title", "")[:30] for n in news_items[:3]]) if news_items else "无新闻"

    detail = "，".join(factors) if factors else "外围市场无明显变化"

    logger.info(f"隔夜风险信号: {score:.0f}分 ({risk_level}) - {detail}")

    return OvernightSignal(
        score=score,
        risk_level=risk_level,
        detail=detail,
        us_summary=us_summary,
        a50_summary=a50_summary,
        news_summary=news_summary,
    )
