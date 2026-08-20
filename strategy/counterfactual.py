"""候选反事实学习。

交易系统不能只从实际成交学习：每个被 HOLD、过滤或未执行的候选都应在
后续行情中得到结果，才能识别错误观望和真正有效的风险过滤。
"""
import json
import logging
from datetime import datetime
from typing import Dict, List

logger = logging.getLogger("strategy.counterfactual")

HORIZONS = (1, 3, 5, 10)


def record_trade_plan_candidates(plan, db_path: str = None) -> int:
    """将一份 TradePlan 中的候选持久化为当日反事实观察样本。"""
    raw_scores = list(getattr(plan, "raw_scores", []) or [])
    if not raw_scores:
        return 0

    orders_by_code = {str(order.code): order for order in (getattr(plan, "orders", []) or [])}
    hold_reasons = getattr(plan, "hold_reasons", {}) or {}
    now = datetime.now().isoformat()
    saved = 0

    try:
        from data.database import Database
        with Database(db_path=db_path) as db:
            for rank, score in enumerate(raw_scores, start=1):
                code = str(score.get("code") or "").strip()
                price = score.get("latest_price")
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    price = 0.0
                if not code or price <= 0:
                    continue

                order = orders_by_code.get(code)
                llm_action = str(score.get("llm_action") or "").upper()
                action = str(getattr(order, "action", "") or llm_action or "HOLD").upper()
                if action not in ("BUY", "SELL", "HOLD"):
                    action = "HOLD"
                db.upsert_candidate_outcome({
                    "observation_date": getattr(plan, "date", ""),
                    "observed_at": now,
                    "code": code,
                    "name": str(score.get("name") or code),
                    "strategy_version": str(getattr(plan, "strategy_version", "") or ""),
                    "regime": str(getattr(plan, "regime", "") or ""),
                    "action": action,
                    "llm_action": llm_action,
                    "llm_confidence": score.get("llm_confidence"),
                    "score": score.get("composite"),
                    "entry_price": price,
                    "hold_reason": str(hold_reasons.get(code) or score.get("llm_reason") or ""),
                    "dimensions": json.dumps(score.get("dimensions") or {}, ensure_ascii=False),
                    "source": f"scan_rank_{rank}",
                })
                saved += 1
    except Exception as exc:
        logger.warning("候选反事实观察写入失败(非致命): %s", exc)
        return 0
    return saved


def _return(entry_price: float, row: Dict) -> float:
    try:
        close = float(row.get("close") or 0)
        return round(close / entry_price - 1, 6) if entry_price > 0 and close > 0 else None
    except (TypeError, ValueError):
        return None


def _label(action: str, return_5d: float) -> str:
    if return_5d is None:
        return ""
    if action == "HOLD":
        if return_5d >= 0.05:
            return "missed_opportunity"
        if return_5d <= -0.03:
            return "avoided_loss"
        return "neutral_hold"
    if action == "BUY":
        return "validated_buy" if return_5d > 0 else "failed_buy"
    return "observed_sell"


def evaluate_candidate_outcomes(limit: int = 1000, db_path: str = None) -> Dict[str, int]:
    """盘后用已落库日线回填候选后续表现，不触发任何外部请求。"""
    result = {"checked": 0, "updated": 0, "matured": 0}
    try:
        from data.database import Database
        with Database(db_path=db_path) as db:
            observations = db.get_candidate_outcomes(pending_only=True, limit=limit)
            for observation in observations:
                result["checked"] += 1
                entry_price = float(observation.get("entry_price") or 0)
                if entry_price <= 0:
                    continue
                rows = db.get_k_daily(
                    observation["code"],
                    observation["observation_date"],
                    "2999-12-31",
                )
                future = [row for row in rows if str(row.get("date") or "") > observation["observation_date"]]
                if not future:
                    continue

                updates = {"evaluated_at": datetime.now().isoformat()}
                for horizon in HORIZONS:
                    if len(future) >= horizon:
                        updates[f"return_{horizon}d"] = _return(entry_price, future[horizon - 1])

                window = future[:5]
                if window:
                    highs = [float(row.get("high") or 0) for row in window if float(row.get("high") or 0) > 0]
                    lows = [float(row.get("low") or 0) for row in window if float(row.get("low") or 0) > 0]
                    if highs:
                        updates["mfe_5d"] = round(max(highs) / entry_price - 1, 6)
                    if lows:
                        updates["mae_5d"] = round(min(lows) / entry_price - 1, 6)

                return_5d = updates.get("return_5d")
                if return_5d is not None:
                    updates["outcome_label"] = _label(str(observation.get("action") or "HOLD"), return_5d)
                    result["matured"] += 1
                db.update_candidate_outcome(int(observation["id"]), updates)
                result["updated"] += 1
    except Exception as exc:
        logger.warning("候选反事实结果回填失败(非致命): %s", exc)
    return result


def summarize_candidate_outcomes(days: int = 30, db_path: str = None) -> Dict[str, int]:
    """给日报/看板提供最小可解释摘要。"""
    try:
        from data.database import Database
        with Database(db_path=db_path) as db:
            rows = db.conn.execute("""
                SELECT outcome_label, COUNT(*) AS count
                FROM candidate_outcomes
                WHERE outcome_label <> ''
                  AND observation_date >= date('now', ?)
                GROUP BY outcome_label
            """, (f"-{max(1, int(days))} days",)).fetchall()
        return {str(row["outcome_label"]): int(row["count"]) for row in rows}
    except Exception:
        return {}
