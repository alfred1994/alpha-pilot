"""同日 LLM HOLD 结论复用。

盘中每 30 分钟扫描一次，技术面在盘中冻结，同一只股票当天常被重复送进 LLM
（2026-10-28 同一只 301314 被判断 7 次，结论全部 HOLD）。输入没有实质变化时，
重复调用只消耗时间预算，不产生新信息。

这里只复用 HOLD：同一交易日、同一策略版本与市场状态、综合分与价格变化都在
阈值内、且未超过复用窗口时，直接沿用上一次结论。BUY/SELL 永远重新判断。
"""
import json
import logging
import os
import time
from typing import Optional

from config import DATA_DIR, LLM_HOLD_REUSE_MAX_SCORE_DELTA, LLM_HOLD_REUSE_SECONDS

logger = logging.getLogger("strategy.llm_hold_cache")

LLM_HOLD_CACHE_FILE = os.path.join(DATA_DIR, "llm_hold_cache.json")
MAX_PRICE_CHANGE = 0.02


def _state_file() -> str:
    override = os.environ.get("ALPHAPILOT_RISK_STATE_DIR")
    if override:
        return os.path.join(override, os.path.basename(LLM_HOLD_CACHE_FILE))
    return LLM_HOLD_CACHE_FILE


def _read(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def lookup(code: str, *, date: str, directive_version: str, regime: str,
           composite: float, price: float = None, now_ts: float = None,
           state_file: str = None, window_seconds: int = None,
           max_score_delta: float = None) -> Optional[dict]:
    """返回可复用的 HOLD 记录；任何条件不满足都返回 None，由调用方正常调用 LLM。"""
    window = LLM_HOLD_REUSE_SECONDS if window_seconds is None else window_seconds
    if window <= 0:
        return None
    delta = LLM_HOLD_REUSE_MAX_SCORE_DELTA if max_score_delta is None else max_score_delta
    data = _read(state_file or _state_file())
    if data.get("date") != date:
        return None
    entry = (data.get("items") or {}).get(str(code))
    if not isinstance(entry, dict):
        return None
    now_ts = time.time() if now_ts is None else now_ts
    try:
        if entry.get("directive_version") != (directive_version or "") or entry.get("regime") != (regime or ""):
            return None
        if now_ts - float(entry["at"]) > window or now_ts < float(entry["at"]):
            return None
        if abs(float(composite) - float(entry["composite"])) > delta:
            return None
        old_price = float(entry.get("price") or 0)
        if price and old_price > 0 and abs(float(price) / old_price - 1) > MAX_PRICE_CHANGE:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return entry


def remember(code: str, *, date: str, directive_version: str, regime: str,
             composite: float, price: float = None, reason: str = "",
             confidence: float = 0.0, decision_id=None, now_ts: float = None,
             state_file: str = None) -> None:
    """记录一次真实 LLM HOLD；写失败只影响复用，不影响交易链路。"""
    from scheduler.persistence import update_json

    entry = {
        "at": time.time() if now_ts is None else now_ts,
        "directive_version": directive_version or "",
        "regime": regime or "",
        "composite": float(composite),
        "price": float(price) if price else None,
        "reason": str(reason or "")[:300],
        "confidence": float(confidence or 0),
        "decision_id": decision_id,
    }

    def _update(previous):
        previous = previous if isinstance(previous, dict) else {}
        items = previous.get("items") if previous.get("date") == date else {}
        items = dict(items or {})
        items[str(code)] = entry
        return {"date": date, "items": items}

    try:
        update_json(state_file or _state_file(), _update)
    except Exception as exc:  # noqa: BLE001 - 缓存失败不得阻断扫描
        logger.warning("LLM HOLD 复用记录写入失败(非致命): %s", type(exc).__name__)
