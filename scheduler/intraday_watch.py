"""
盘中轻量盯盘与踏空检测
====================================================================
该模块不做全市场慢扫描，也不直接下单。它维护一个观察池:
  - 每轮轻量看盘更新 watchlist
  - 识别疑似踏空
  - 只有二次确认的观察标的才允许进入救援执行
====================================================================
"""
import json
import os
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from config import (
    AUTO_RESCUE_MAX_TOPK,
    AUTO_RESCUE_POSITION_SCALE,
    AUTO_WATCHLIST_TTL,
    DATA_DIR,
)
from scheduler import logger


WATCHLIST_FILE = os.path.join(DATA_DIR, "intraday_watchlist.json")
WATCHLIST_MIN_SCORE = 50
RESCUE_CONFIRMATIONS = 2
RESCUE_CUTOFF_HHMM = (14, 30)
HIGH_CASH_RATIO = 0.35
LIMIT_UP_ABS_DELTA = 20
LIMIT_UP_REL_DELTA = 0.5


def _today(now: datetime = None) -> str:
    now = now or datetime.now()
    return now.strftime("%Y-%m-%d")


def _allow_new_entries(now: datetime) -> bool:
    return (now.hour, now.minute) < RESCUE_CUTOFF_HHMM


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value in ("-", "--", None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _candidate_to_item(candidate: Dict, now_ts: float, reason: str) -> Dict:
    return {
        "code": str(candidate.get("code", "")).strip(),
        "name": str(candidate.get("name", "")),
        "score": _safe_float(candidate.get("score", candidate.get("composite", 0))),
        "source": candidate.get("source", []),
        "first_seen": now_ts,
        "last_seen": now_ts,
        "confirmations": 1,
        "reason": reason,
        "status": "watching",
    }


def load_watchlist(now: datetime = None, now_ts: float = None,
                   path: str = None, ttl: int = None) -> Dict:
    """读取观察池并清理过期数据。"""
    path = path or WATCHLIST_FILE
    ttl = ttl or AUTO_WATCHLIST_TTL
    today = _today(now)
    if not os.path.exists(path):
        return {"date": today, "items": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"date": today, "items": {}}

    if data.get("date") != today:
        return {"date": today, "items": {}}

    now_ts = now_ts if now_ts is not None else time.time()
    items = {}
    for code, item in (data.get("items") or {}).items():
        last_seen = _safe_float(item.get("last_seen"), 0)
        if last_seen and now_ts - last_seen <= ttl:
            items[code] = item
    return {"date": today, "items": items}


def save_watchlist(data: Dict, path: str = None):
    """保存观察池。"""
    path = path or WATCHLIST_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_default_market_snapshot() -> Dict:
    try:
        from data.snapshot import get_market_snapshot
        return get_market_snapshot(max_age=900) or {}
    except Exception as e:
        logger.debug(f"轻量看盘读取市场快照失败: {e}")
        return {}


def _load_default_candidate_pool() -> List[Dict]:
    try:
        from data.snapshot import get_candidate_pool
        pool = get_candidate_pool(max_age=1200) or {}
        return pool.get("candidates") or []
    except Exception as e:
        logger.debug(f"轻量看盘读取候选池失败: {e}")
        return []


def _load_default_account_snapshot() -> Dict:
    try:
        from execution.paper_account import PaperAccount
        account = PaperAccount()
        total_assets = account.total_assets()
        return {
            "cash": account.cash,
            "total_assets": total_assets,
            "position_count": account.position_count,
            "positions": account.positions,
        }
    except Exception as e:
        logger.debug(f"轻量看盘读取账户失败: {e}")
        return {"cash": 0, "total_assets": 0, "position_count": 0, "positions": {}}


def _build_baseline(market: Dict, candidates: List[Dict], account: Dict, now_ts: float) -> Dict:
    """构造早盘基准。"""
    total_assets = _safe_float(account.get("total_assets"))
    cash = _safe_float(account.get("cash"))
    cash_ratio = cash / total_assets if total_assets > 0 else 0.0
    return {
        "created_at": now_ts,
        "limit_up_count": len(market.get("limit_up", []) or []),
        "limit_down_count": len(market.get("limit_down", []) or []),
        "candidate_count": len(candidates),
        "cash_ratio": round(cash_ratio, 4),
        "position_count": int(account.get("position_count") or 0),
    }


def _market_turning_stronger(market: Dict, baseline: Dict) -> tuple:
    """判断市场是否相对早盘明显转强。"""
    current_up = len(market.get("limit_up", []) or [])
    base_up = int(baseline.get("limit_up_count") or 0)
    if base_up <= 0:
        stronger = current_up >= LIMIT_UP_ABS_DELTA
    else:
        stronger = (
            current_up - base_up >= LIMIT_UP_ABS_DELTA
            or (current_up - base_up) / max(base_up, 1) >= LIMIT_UP_REL_DELTA
        )
    reason = f"涨停数 {base_up}->{current_up}"
    return stronger, reason


def _update_watchlist(
    watchlist: Dict,
    candidates: List[Dict],
    now: datetime,
    now_ts: float,
    allow_new: bool,
) -> Dict:
    """用候选池刷新观察池。"""
    items = dict(watchlist.get("items") or {})
    seen = set()
    sorted_candidates = sorted(
        candidates,
        key=lambda c: _safe_float(c.get("score", c.get("composite", 0))),
        reverse=True,
    )
    for candidate in sorted_candidates[: max(AUTO_RESCUE_MAX_TOPK * 2, 10)]:
        code = str(candidate.get("code", "")).strip()
        if not code:
            continue
        score = _safe_float(candidate.get("score", candidate.get("composite", 0)))
        if score < WATCHLIST_MIN_SCORE:
            continue
        seen.add(code)
        if code in items:
            items[code]["last_seen"] = now_ts
            items[code]["score"] = max(_safe_float(items[code].get("score")), score)
            items[code]["confirmations"] = int(items[code].get("confirmations") or 1) + 1
            items[code]["status"] = "confirmed" if items[code]["confirmations"] >= RESCUE_CONFIRMATIONS else "watching"
        elif allow_new:
            items[code] = _candidate_to_item(candidate, now_ts, "候选池接近交易门槛")

    # 没有在本轮出现的旧观察标的保留到TTL，便于二次确认失败时解释。
    for code, item in items.items():
        if code not in seen and item.get("status") == "confirmed":
            item["status"] = "cooling"

    return {"date": _today(now), "items": items}


def run_watch_cycle(
    *,
    state,
    now: datetime,
    now_ts: float,
    services: Optional[Dict[str, Callable]] = None,
    watchlist_path: str = None,
) -> Dict:
    """
    执行一次轻量看盘。

    Returns:
        dict: actions/details/watchlist/missed_opportunity/rescue_requested
    """
    services = services or {}
    market_loader = services.get("watch_market_snapshot", _load_default_market_snapshot)
    candidate_loader = services.get("watch_candidate_pool", _load_default_candidate_pool)
    account_loader = services.get("watch_account_snapshot", _load_default_account_snapshot)

    market = market_loader() or {}
    candidates = candidate_loader() or []
    account = account_loader() or {}
    allow_new = _allow_new_entries(now)

    baseline = state.morning_baseline or {}
    if not baseline:
        baseline = _build_baseline(market, candidates, account, now_ts)
        state.morning_baseline = baseline

    watchlist = load_watchlist(now=now, now_ts=now_ts, path=watchlist_path)
    watchlist = _update_watchlist(watchlist, candidates, now, now_ts, allow_new)
    save_watchlist(watchlist, path=watchlist_path)

    total_assets = _safe_float(account.get("total_assets"))
    cash = _safe_float(account.get("cash"))
    cash_ratio = cash / total_assets if total_assets > 0 else 0.0
    position_count = int(account.get("position_count") or 0)
    market_stronger, market_reason = _market_turning_stronger(market, baseline)
    high_cash = cash_ratio >= HIGH_CASH_RATIO or position_count == 0
    confirmed = [
        item for item in watchlist["items"].values()
        if int(item.get("confirmations") or 0) >= RESCUE_CONFIRMATIONS
        and item.get("status") == "confirmed"
    ]

    missed = bool(high_cash and (market_stronger or confirmed))
    rescue_requested = bool(missed and confirmed and allow_new)

    top_watch = sorted(
        watchlist["items"].values(),
        key=lambda item: (_safe_float(item.get("score")), int(item.get("confirmations") or 0)),
        reverse=True,
    )[:AUTO_RESCUE_MAX_TOPK]

    actions = [
        f"轻量看盘: 候选{len(candidates)}只 观察{len(watchlist['items'])}只 "
        f"确认{len(confirmed)}只 现金{cash_ratio:.0%} {market_reason}"
    ]
    if missed:
        actions.append("疑似踏空: 市场转强/高现金/观察池确认，等待救援确认")
    if not allow_new:
        actions.append("14:30后不新增追入机会，仅刷新观察池")

    return {
        "actions": actions,
        "details": {
            "baseline": baseline,
            "limit_up_count": len(market.get("limit_up", []) or []),
            "limit_down_count": len(market.get("limit_down", []) or []),
            "candidate_count": len(candidates),
            "cash_ratio": round(cash_ratio, 4),
            "position_count": position_count,
            "market_stronger": market_stronger,
            "market_reason": market_reason,
            "high_cash": high_cash,
            "allow_new_entries": allow_new,
            "confirmed_count": len(confirmed),
            "top_watch": top_watch,
        },
        "watchlist": watchlist,
        "missed_opportunity": missed,
        "rescue_requested": rescue_requested,
        "eligible_codes": [item["code"] for item in confirmed[:AUTO_RESCUE_MAX_TOPK]],
    }


def filter_trade_plan_for_rescue(plan_data: Dict, eligible_codes: List[str]) -> Dict:
    """救援扫描只允许执行二次确认观察池里的订单，并缩小仓位。"""
    if not plan_data:
        return plan_data
    eligible = set(eligible_codes or [])
    filtered = dict(plan_data)
    kept_orders = []
    hold_reasons = dict(filtered.get("hold_reasons") or {})
    for order in filtered.get("orders", []) or []:
        code = order.get("code", "")
        if order.get("action") == "BUY" and code not in eligible:
            hold_reasons[code] = "HOLD_RESCUE_NOT_CONFIRMED"
            continue
        if order.get("action") == "BUY":
            order = dict(order)
            order["target_weight"] = round(_safe_float(order.get("target_weight")) * AUTO_RESCUE_POSITION_SCALE, 4)
            order["reason"] = f"救援扫描小仓: {order.get('reason', '')}".strip()
        kept_orders.append(order)
    filtered["orders"] = kept_orders
    filtered["hold_reasons"] = hold_reasons
    return filtered
