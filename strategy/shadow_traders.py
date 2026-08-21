"""
影子策略变体框架
====================================================================
在同一个候选池和同一份打分结果上，并行推导多套策略参数下的
"如果这样交易"决策，落库后复用反事实回填的净收益计算对比指标。

设计约束（对应原始规划第4项）：
- 所有影子变体共享同一份行情、特征与综合打分，不重复访问外部接口，
  不产生额外 LLM 请求。
- v1 虚拟账户口径为日度等权篮子 + T+5 净收益（candidate_outcomes），
  不做盘中持仓级模拟。
- 影子决策永不改变真实下单。
====================================================================
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("strategy.shadow_traders")

# ── 变体定义：相对正式策略的参数/权重偏差 ──
# top_k_delta / min_score_delta 作用于选股门槛；
# ml_weight_factor 对 ml 维度权重缩放后重算综合分；
# mode="ml_only" 表示完全按 ml 分数排序。
SHADOW_VARIANTS: Dict[str, dict] = {
    "baseline": {},
    "loose_top": {"top_k_delta": 1, "min_score_delta": -3.0},
    "strict_top": {"top_k_delta": -1, "min_score_delta": 3.0},
    "ml_heavy": {"ml_weight_factor": 2.0},
    "ml_none": {"ml_weight_factor": 0.0},
    "ml_only": {"mode": "ml_only"},
}
SHADOW_VARIANT_IDS: List[str] = list(SHADOW_VARIANTS)

SHADOW_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS shadow_decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id       TEXT NOT NULL,
    decision_date    TEXT NOT NULL,
    code             TEXT NOT NULL,
    action           TEXT NOT NULL,
    adjusted_score   REAL,
    rank             INTEGER,
    hold_reason      TEXT DEFAULT '',
    price            REAL,
    regime           TEXT,
    strategy_version TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE(variant_id, decision_date, code)
)
"""


def _adjust_score(entry: dict, variant: dict) -> float:
    """按变体口径重算分数；无权重偏差时直接用综合分。"""
    if variant.get("mode") == "ml_only":
        ml = (entry.get("dimensions") or {}).get("ml") or {}
        return float(ml.get("score", 50.0))
    factor = variant.get("ml_weight_factor")
    if factor is None:
        return float(entry.get("composite", 50.0))
    from config import SIGNAL_WEIGHTS
    dims = entry.get("dimensions") or {}
    weighted_sum = 0.0
    weight_total = 0.0
    for name, weight in SIGNAL_WEIGHTS.items():
        if name not in dims:
            continue
        effective_weight = weight * (factor if name == "ml" else 1.0)
        weighted_sum += effective_weight * float(dims[name].get("score", 50.0))
        weight_total += effective_weight
    return weighted_sum / weight_total if weight_total > 0 else 50.0


def decide_variant_actions(scored: List[dict], base_params: dict,
                           variant_id: str) -> Dict[str, dict]:
    """
    在共享打分结果上推导一个变体的买卖决策。

    选股逻辑与真实计划一致：按调整分降序取前 top_k，且 adjusted_score
    须过 min_score；其余标记 HOLD 原因。

    Returns:
        {code: {action, adjusted_score, rank, hold_reason}}
    """
    variant = SHADOW_VARIANTS.get(variant_id)
    if variant is None:
        raise ValueError(f"未知影子变体: {variant_id}")
    top_k = max(1, int(base_params.get("top_k", 3)) + int(variant.get("top_k_delta", 0)))
    min_score = float(base_params.get("min_score", 58.0)) + float(variant.get("min_score_delta", 0.0))

    ranked = sorted(
        ((entry, _adjust_score(entry, variant)) for entry in (scored or [])),
        key=lambda pair: pair[1],
        reverse=True,
    )
    decisions: Dict[str, dict] = {}
    for rank, (entry, adjusted) in enumerate(ranked, start=1):
        code = str(entry.get("code", ""))
        if rank <= top_k and adjusted >= min_score:
            decisions[code] = {
                "action": "BUY", "adjusted_score": round(adjusted, 1),
                "rank": rank, "hold_reason": "",
            }
        elif rank <= top_k:
            decisions[code] = {
                "action": "HOLD", "adjusted_score": round(adjusted, 1),
                "rank": rank,
                "hold_reason": f"HOLD_SCORE_LOW({adjusted:.0f}<{min_score:g})",
            }
        else:
            decisions[code] = {
                "action": "HOLD", "adjusted_score": round(adjusted, 1),
                "rank": rank,
                "hold_reason": f"HOLD_NOT_TOP{top_k}(score={adjusted:.0f})",
            }
    return decisions


def ensure_tables(db) -> None:
    db.conn.execute(SHADOW_DECISIONS_DDL)
    db.conn.commit()


def record_daily_decisions(db, decision_date: str, regime: str,
                           strategy_version: str, scored: List[dict],
                           base_params: dict) -> Dict[str, dict]:
    """
    记录一轮扫描中全部变体的影子决策（同日同变体同股票幂等）。

    Returns:
        {variant_id: {"buys": n, "holds": n}}
    """
    ensure_tables(db)
    now = datetime.now().isoformat()
    summary: Dict[str, dict] = {}
    for variant_id in SHADOW_VARIANT_IDS:
        decisions = decide_variant_actions(scored, base_params, variant_id)
        buys = holds = 0
        for code, item in decisions.items():
            price = None
            for entry in scored or []:
                if str(entry.get("code", "")) == code:
                    price = float(entry.get("latest_price") or 0) or None
                    break
            db.conn.execute(
                "INSERT OR IGNORE INTO shadow_decisions"
                " (variant_id, decision_date, code, action, adjusted_score,"
                "  rank, hold_reason, price, regime, strategy_version, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (variant_id, decision_date, code, item["action"],
                 item["adjusted_score"], item["rank"], item["hold_reason"],
                 price, regime, strategy_version, now),
            )
            if item["action"] == "BUY":
                buys += 1
            else:
                holds += 1
        summary[variant_id] = {"buys": buys, "holds": holds}
    db.conn.commit()
    logger.info(
        "[影子] %s 决策已记录: %s",
        decision_date,
        ", ".join(f"{v}={s['buys']}买{s['holds']}持" for v, s in summary.items()),
    )
    return summary


def _load_outcome_map(db) -> Dict[tuple, float]:
    """(observation_date, code) → net_return_5d；同键多扫描取最新一条。"""
    rows = db.conn.execute(
        "SELECT id, observation_date, code, net_return_5d FROM candidate_outcomes"
        " WHERE net_return_5d IS NOT NULL ORDER BY id"
    ).fetchall()
    outcome_map: Dict[tuple, float] = {}
    for row_id, date, code, ret in rows:
        outcome_map[(str(date), str(code))] = float(ret)
    return outcome_map


def compute_variant_metrics(db, variant_id: str) -> dict:
    """
    汇总一个变体的影子绩效指标（基于 T+5 净收益反事实回填）。

    error_hold_rate：HOLD 且后续5日上涨的比例——错过的机会，
    是日终策略应重点学习的对象。
    """
    decisions = db.conn.execute(
        "SELECT decision_date, code, action FROM shadow_decisions WHERE variant_id=?",
        (variant_id,),
    ).fetchall()
    outcome_map = _load_outcome_map(db)

    buy_returns: List[float] = []
    hold_returns: List[float] = []
    pending_buys = 0
    for date, code, action in decisions:
        ret = outcome_map.get((str(date), str(code)))
        if ret is None:
            if action == "BUY":
                pending_buys += 1
            continue
        if action == "BUY":
            buy_returns.append(ret)
        else:
            hold_returns.append(ret)

    wins = [r for r in buy_returns if r > 0]
    losses = [r for r in buy_returns if r <= 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    profit_loss_ratio = (
        round(avg_win / abs(avg_loss), 4)
        if avg_win is not None and avg_loss not in (None, 0) else None
    )
    return {
        "variant_id": variant_id,
        "trading_days": len({str(d) for d, _, _ in decisions}),
        "buys": len(buy_returns),
        "holds": len(hold_returns),
        "pending_buys": pending_buys,
        "avg_net_5d": round(sum(buy_returns) / len(buy_returns), 6) if buy_returns else None,
        "win_rate": round(len(wins) / len(buy_returns), 4) if buy_returns else None,
        "profit_loss_ratio": profit_loss_ratio,
        "error_hold_rate": (
            round(sum(1 for r in hold_returns if r > 0) / len(hold_returns), 4)
            if hold_returns else None
        ),
        "coverage": round(len(buy_returns) / max(1, len(buy_returns) + len(hold_returns)), 4),
    }
