"""候选反事实学习。

交易系统不能只从实际成交学习：每个被 HOLD、过滤或未执行的候选都应在
后续行情中得到结果，才能识别错误观望和真正有效的风险过滤。
"""
import json
import hashlib
import logging
import os
import uuid
from datetime import datetime
from typing import Dict, List

logger = logging.getLogger("strategy.counterfactual")

HORIZONS = (1, 3, 5, 10)
COUNTERFACTUAL_SLIPPAGE_RATE = float(os.environ.get("COUNTERFACTUAL_SLIPPAGE_RATE", "0.0005"))


def _scan_id(plan, observed_at: str) -> str:
    """取得一次扫描的稳定标识；同一计划重试不能重复取样。"""
    scan_id = str(getattr(plan, "scan_id", "") or "").strip()
    if scan_id:
        return scan_id
    scan_id = f"{getattr(plan, 'date', '')}T{observed_at[11:26]}-{uuid.uuid4().hex[:12]}"
    try:
        setattr(plan, "scan_id", scan_id)
    except Exception:
        # 兼容只读的旧计划对象；这类调用仍有随机扫描标识，但不会覆盖原记录。
        pass
    return scan_id


def _denial_layer(action: str, hold_reason: str, llm_action: str) -> str:
    """将扫描阶段已知的拒绝原因归类；执行结果必须由执行审计另行产生。"""
    reason = str(hold_reason or "").upper()
    if reason.startswith("HOLD_SCORE_LOW"):
        return "score_gate"
    if reason.startswith("HOLD_NOT_TOP"):
        return "ranking_gate"
    if reason.startswith("HOLD_LLM") or reason.startswith("HOLD_NO_BUY_DECISION"):
        return "llm"
    if str(action or "").upper() == "HOLD" or str(llm_action or "").upper() == "HOLD":
        return "hold_unknown"
    return ""


def record_trade_plan_candidates(plan, db_path: str = None) -> int:
    """将一份 TradePlan 中的候选持久化为当日反事实观察样本。"""
    raw_scores = list(getattr(plan, "raw_scores", []) or [])
    if not raw_scores:
        return 0

    orders_by_code = {str(order.code): order for order in (getattr(plan, "orders", []) or [])}
    hold_reasons = getattr(plan, "hold_reasons", {}) or {}
    now = datetime.now().isoformat()
    scan_id = _scan_id(plan, now)
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
                strategy_version = str(getattr(plan, "strategy_version", "") or "")
                # 同一扫描中同一代码只有一个观察；重试时保持 observation_key 不变，
                # 由数据库幂等写入防止同一判断被重复学习。
                key_payload = f"{scan_id}|{code}"
                observation_key = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()[:24]
                from config import COMMISSION_RATE, STAMP_TAX_RATE
                hold_reason = str(hold_reasons.get(code) or score.get("llm_reason") or "")
                outcome_id = db.insert_candidate_outcome({
                    "observation_key": observation_key,
                    "scan_id": scan_id,
                    "observation_date": getattr(plan, "date", ""),
                    "observed_at": now,
                    "code": code,
                    "name": str(score.get("name") or code),
                    "strategy_version": strategy_version,
                    "regime": str(getattr(plan, "regime", "") or ""),
                    "action": action,
                    "llm_action": llm_action,
                    "llm_confidence": score.get("llm_confidence"),
                    "score": score.get("composite"),
                    "entry_price": price,
                    "price_source": str(score.get("price_source") or "daily_close"),
                    "hold_reason": hold_reason,
                    "denial_layer": _denial_layer(action, hold_reason, llm_action),
                    "dimensions": json.dumps(score.get("dimensions") or {}, ensure_ascii=False),
                    "source": f"scan_rank_{rank}",
                    "fee_rate": COMMISSION_RATE * 2 + STAMP_TAX_RATE,
                    "slippage_rate": COUNTERFACTUAL_SLIPPAGE_RATE,
                })
                if outcome_id:
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


def _label(action: str, net_return_5d: float) -> str:
    if net_return_5d is None:
        return ""
    if str(action or "").upper() == "HOLD":
        if net_return_5d >= 0.05:
            return "missed_opportunity"
        if net_return_5d <= -0.03:
            return "avoided_loss"
        return "neutral_hold"
    if str(action or "").upper() == "BUY":
        return "validated_buy" if net_return_5d > 0 else "failed_buy"
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

                updates = {}
                total_cost = float(observation.get("fee_rate") or 0) + 2 * float(
                    observation.get("slippage_rate") or 0
                )
                for horizon in HORIZONS:
                    if len(future) >= horizon:
                        gross_return = _return(entry_price, future[horizon - 1])
                        if gross_return is not None:
                            gross_key = f"return_{horizon}d"
                            net_key = f"net_return_{horizon}d"
                            net_return = round(gross_return - total_cost, 6)
                            if observation.get(gross_key) != gross_return:
                                updates[gross_key] = gross_return
                            if observation.get(net_key) != net_return:
                                updates[net_key] = net_return

                # MFE/MAE 的窗口必须完整成熟；不足五个交易日不能把临时极值
                # 冒充 T+5 统计量。
                window = future[:5] if len(future) >= 5 else []
                if window:
                    highs = [float(row.get("high") or 0) for row in window if float(row.get("high") or 0) > 0]
                    lows = [float(row.get("low") or 0) for row in window if float(row.get("low") or 0) > 0]
                    if highs:
                        mfe = round(max(highs) / entry_price - 1, 6)
                        if observation.get("mfe_5d") != mfe:
                            updates["mfe_5d"] = mfe
                    if lows:
                        mae = round(min(lows) / entry_price - 1, 6)
                        if observation.get("mae_5d") != mae:
                            updates["mae_5d"] = mae

                net_return_5d = updates.get("net_return_5d", observation.get("net_return_5d"))
                if net_return_5d is not None and not observation.get("outcome_label"):
                    updates["outcome_label"] = _label(
                        str(observation.get("action") or "HOLD"), net_return_5d
                    )
                    result["matured"] += 1
                if updates:
                    updates["evaluated_at"] = datetime.now().isoformat()
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
