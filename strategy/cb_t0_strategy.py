"""
可转债T+0策略引擎
正股涨停→资金转向可转债→T+0日内套利

评分体系（参照OpenClaw文档）:
  溢价率    40%  <5%最优
  跟随度    20%  转债涨幅/正股涨幅 > 80%
  剩余规模  20%  <5亿最优
  换手率    15%  >20%最优
  量比       5%  >2最优

风控纪律:
  - 严禁隔夜持仓
  - 溢价>30%不追
  - 正股炸板立即止盈
  - 止损-3%
  - 单只仓位≤20%
"""
import logging
from typing import Dict, List, Optional
from datetime import datetime

from config import (
    CB_MAX_PREMIUM, CB_STOP_LOSS, CB_SINGLE_POSITION,
    CB_MIN_SCORE, CB_T0_ENABLED,
)

logger = logging.getLogger("strategy.cb_t0")


# ── 评分权重 ─────────────────────────────────────────────────
WEIGHTS = {
    "premium": 0.40,       # 溢价率
    "following": 0.20,     # 跟随度
    "scale": 0.20,         # 剩余规模
    "turnover": 0.15,      # 换手率
    "volume_ratio": 0.05,  # 量比
}


def score_cb(cb: dict) -> dict:
    """
    对单只可转债评分

    Args:
        cb: 可转债数据字典（来自 convertible_bond.get_cb_list）

    Returns:
        {total_score, details, signal, action}
    """
    details = {}

    # 1. 溢价率评分 (40%) — 越低越好，<5%满分
    premium = cb.get("premium_rate", 100)
    if premium <= 0:
        details["premium"] = 100  # 负溢价 = 折价，极好
    elif premium < 5:
        details["premium"] = 100
    elif premium < 10:
        details["premium"] = 90 - (premium - 5) * 8
    elif premium < 20:
        details["premium"] = 50 - (premium - 10) * 3
    elif premium < 30:
        details["premium"] = 20 - (premium - 20) * 1.5
    else:
        details["premium"] = 0  # 溢价>30%不追

    # 2. 跟随度评分 (20%) — 转债涨幅/正股涨幅，>80%满分
    stock_chg = abs(cb.get("stock_change_pct", 0))
    cb_chg = abs(cb.get("cb_change_pct", 0))
    if stock_chg > 0.5:  # 正股涨幅>0.5%才有意义
        following = min(cb_chg / stock_chg * 100, 150)  # 上限150%
        if following >= 80:
            details["following"] = 100
        elif following >= 50:
            details["following"] = 60 + (following - 50) * 1.33
        else:
            details["following"] = following * 1.2
    else:
        details["following"] = 50  # 正股没动，中性

    # 3. 剩余规模评分 (20%) — 越小越好，<5亿满分
    scale = cb.get("remaining_scale", 100)
    if scale <= 0:
        details["scale"] = 50  # 数据缺失
    elif scale < 2:
        details["scale"] = 100
    elif scale < 5:
        details["scale"] = 80 + (5 - scale) * 6.67
    elif scale < 10:
        details["scale"] = 50 + (10 - scale) * 6
    else:
        details["scale"] = max(0, 50 - (scale - 10) * 2)

    # 4. 换手率评分 (15%) — 越高越好，>20%满分
    turnover = cb.get("turnover_rate", 0)
    if turnover >= 20:
        details["turnover"] = 100
    elif turnover >= 10:
        details["turnover"] = 70 + (turnover - 10) * 3
    elif turnover >= 5:
        details["turnover"] = 50 + (turnover - 5) * 4
    elif turnover >= 1:
        details["turnover"] = 20 + (turnover - 1) * 7.5
    else:
        details["turnover"] = turnover * 20

    # 5. 量比评分 (5%) — >2满分（简化：用成交额/剩余规模估算）
    trade_amount = cb.get("trade_amount", 0)  # 万元
    remaining = cb.get("remaining_scale", 1) * 10000  # 转为万元
    if remaining > 0 and trade_amount > 0:
        vol_ratio = trade_amount / remaining
        if vol_ratio >= 0.02:  # 成交额占规模2%以上
            details["volume_ratio"] = 100
        elif vol_ratio >= 0.01:
            details["volume_ratio"] = 70
        else:
            details["volume_ratio"] = 40
    else:
        details["volume_ratio"] = 50

    # 加权总分
    total = sum(
        details.get(k, 0) * v
        for k, v in WEIGHTS.items()
    )

    # 信号判定
    if total >= 80:
        signal = "STRONG_BUY"
        action = "强烈买入"
    elif total >= CB_MIN_SCORE:
        signal = "BUY"
        action = "买入"
    elif total >= 50:
        signal = "WATCH"
        action = "观察"
    else:
        signal = "SKIP"
        action = "跳过"

    return {
        "cb_code": cb.get("cb_code", ""),
        "cb_name": cb.get("cb_name", ""),
        "stock_code": cb.get("stock_code", ""),
        "stock_name": cb.get("stock_name", ""),
        "total_score": round(total, 1),
        "details": {k: round(v, 1) for k, v in details.items()},
        "signal": signal,
        "action": action,
        "premium_rate": premium,
        "stock_change_pct": cb.get("stock_change_pct", 0),
        "cb_change_pct": cb.get("cb_change_pct", 0),
        "cb_price": cb.get("cb_price", 0),
        "remaining_scale": scale,
    }


def scan_and_score() -> List[dict]:
    """
    扫描全市场可转债并评分，返回按分数排序的结果

    Returns:
        评分结果列表（降序）
    """
    if not CB_T0_ENABLED:
        logger.info("可转债T+0策略已禁用")
        return []

    from data.convertible_bond import get_cb_list

    cbs = get_cb_list()
    if not cbs:
        return []

    # 预筛选: 排除明显不合格的
    candidates = []
    for cb in cbs:
        # 排除溢价>30%
        if cb.get("premium_rate", 100) > CB_MAX_PREMIUM * 100:
            continue
        # 排除价格过低（<90）或过高（>200）
        price = cb.get("cb_price", 0)
        if price < 90 or price > 200:
            continue
        # 排除正股跌幅>5%的
        if cb.get("stock_change_pct", 0) < -5:
            continue
        candidates.append(cb)

    # 评分
    scored = []
    for cb in candidates:
        result = score_cb(cb)
        scored.append(result)

    # 按分数降序
    scored.sort(key=lambda x: x["total_score"], reverse=True)

    logger.info(f"可转债评分: {len(scored)}只候选, Top3={[(s['cb_name'], s['total_score']) for s in scored[:3]]}")
    return scored


def should_buy(cb: dict) -> dict:
    """
    买入决策

    Args:
        cb: 评分后的可转债数据

    Returns:
        {buy: bool, reason: str, position_pct: float}
    """
    score = cb.get("total_score", 0)
    premium = cb.get("premium_rate", 100)

    # 溢价>30%不追
    if premium > CB_MAX_PREMIUM * 100:
        return {"buy": False, "reason": f"溢价率{premium:.1f}%>{CB_MAX_PREMIUM*100}%", "position_pct": 0}

    # 分数不够
    if score < CB_MIN_SCORE:
        return {"buy": False, "reason": f"评分{score}<{CB_MIN_SCORE}", "position_pct": 0}

    # 正股涨幅需要>=7%（接近涨停）
    stock_chg = cb.get("stock_change_pct", 0)
    if stock_chg < 7:
        return {"buy": False, "reason": f"正股涨幅{stock_chg:.1f}%<7%", "position_pct": 0}

    # 计算仓位（根据分数动态调整）
    if score >= 85:
        position_pct = CB_SINGLE_POSITION
    elif score >= 75:
        position_pct = CB_SINGLE_POSITION * 0.75
    else:
        position_pct = CB_SINGLE_POSITION * 0.5

    return {
        "buy": True,
        "reason": f"评分{score} 正股+{stock_chg:.1f}% 溢价{premium:.1f}%",
        "position_pct": position_pct,
    }


def should_sell(cb_code: str, current_data: dict, buy_price: float) -> dict:
    """
    卖出决策

    Args:
        cb_code: 转债代码
        current_data: 当前数据 {cb_price, stock_change_pct, premium_rate}
        buy_price: 买入价格

    Returns:
        {sell: bool, reason: str}
    """
    current_price = current_data.get("cb_price", 0)
    if current_price <= 0 or buy_price <= 0:
        return {"sell": False, "reason": "价格数据缺失"}

    pnl = (current_price - buy_price) / buy_price

    # 止损 -3%
    if pnl <= CB_STOP_LOSS:
        return {"sell": True, "reason": f"止损触发: {pnl:+.1%} <= {CB_STOP_LOSS:.0%}"}

    # 正股炸板（涨幅回落到<3%）
    stock_chg = current_data.get("stock_change_pct", 0)
    if stock_chg < 3:
        return {"sell": True, "reason": f"正股炸板: 涨幅回落至{stock_chg:.1f}%"}

    # 溢价扩大到>30%
    premium = current_data.get("premium_rate", 0)
    if premium > 30:
        return {"sell": True, "reason": f"溢价扩大: {premium:.1f}%>30%"}

    return {"sell": False, "reason": "继续持有"}


def format_cb_report(scored: List[dict], top_n: int = 5) -> str:
    """格式化可转债扫描报告"""
    if not scored:
        return "无可转债机会"

    lines = [
        "=" * 55,
        "可转债T+0扫描报告",
        "=" * 55,
    ]

    for i, cb in enumerate(scored[:top_n], 1):
        signal_icon = {"STRONG_BUY": "🟢", "BUY": "🔵", "WATCH": "🟡", "SKIP": "⚪"}.get(cb["signal"], "⚪")
        lines.append(
            f"{signal_icon} #{i} {cb['cb_name']}({cb['cb_code']}) "
            f"分数={cb['total_score']} {cb['action']}"
        )
        lines.append(
            f"   正股: {cb['stock_name']}({cb['stock_code']}) "
            f"涨幅={cb['stock_change_pct']:+.1f}%"
        )
        lines.append(
            f"   转债: 现价={cb['cb_price']:.2f} "
            f"涨幅={cb['cb_change_pct']:+.1f}% "
            f"溢价率={cb['premium_rate']:.1f}%"
        )
        details = cb.get("details", {})
        lines.append(
            f"   评分: 溢价={details.get('premium', 0):.0f} "
            f"跟随={details.get('following', 0):.0f} "
            f"规模={details.get('scale', 0):.0f} "
            f"换手={details.get('turnover', 0):.0f} "
            f"量比={details.get('volume_ratio', 0):.0f}"
        )
        lines.append("")

    lines.append("=" * 55)
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    print("=== 可转债T+0策略测试 ===\n")
    scored = scan_and_score()
    print(format_cb_report(scored))
