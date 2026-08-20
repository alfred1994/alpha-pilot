"""盘后研究股票池与历史K线增量同步。

该模块只补充研究数据，不生成订单、不修改仓位。它把盘中活跃的普通A股
逐步沉淀为宽股票池，供反事实学习和后续 pooled ML 使用。
"""
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List

from config import DATA_DIR

logger = logging.getLogger("data.research_universe")

UNIVERSE_FILE = os.path.join(DATA_DIR, "research_universe.json")
RESEARCH_UNIVERSE_SIZE = int(os.environ.get("RESEARCH_UNIVERSE_SIZE", "300"))
RESEARCH_SYNC_BATCH = int(os.environ.get("RESEARCH_SYNC_BATCH", "12"))
RESEARCH_SYNC_WORKERS = int(os.environ.get("RESEARCH_SYNC_WORKERS", "2"))
RESEARCH_HISTORY_DAYS = int(os.environ.get("RESEARCH_HISTORY_DAYS", "730"))


def _load_universe(path: str = None) -> Dict:
    path = path or UNIVERSE_FILE
    if not os.path.exists(path):
        return {"version": 1, "codes": [], "cursor": 0}
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        return payload if isinstance(payload, dict) else {"version": 1, "codes": [], "cursor": 0}
    except Exception:
        return {"version": 1, "codes": [], "cursor": 0}


def _save_universe(payload: Dict, path: str = None):
    path = path or UNIVERSE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp"
    with open(temp, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


def refresh_research_universe(limit: int = None, path: str = None) -> Dict:
    """以成交额靠前的普通A股构建研究池，保留已有样本以保障连续性。"""
    limit = max(20, int(limit or RESEARCH_UNIVERSE_SIZE))
    from strategy.stock_picker import _get_active_stocks

    active = _get_active_stocks(min_amount=3000, limit=limit)
    current = _load_universe(path)
    ordered: List[Dict] = []
    seen = set()
    for code, name in active.items():
        if code in seen:
            continue
        seen.add(code)
        ordered.append({"code": code, "name": name, "source": "active_liquidity"})
    for item in current.get("codes") or []:
        code = str(item.get("code") or "")
        if code and code not in seen and len(ordered) < limit:
            seen.add(code)
            ordered.append({"code": code, "name": item.get("name", code), "source": "retained"})

    payload = {
        "version": 1,
        "generated_at": datetime.now().isoformat(),
        "codes": ordered[:limit],
        "cursor": min(int(current.get("cursor") or 0), max(0, len(ordered) - 1)),
        "last_sync": current.get("last_sync") or {},
    }
    _save_universe(payload, path)
    return payload


def _sync_one(item: Dict, start_date: str, end_date: str) -> Dict:
    code = str(item.get("code") or "")
    try:
        from data.history import get_daily
        df = get_daily(
            code, start_date=start_date, end_date=end_date,
            require_full_range=True,
        )
        if df is None or df.empty:
            return {"code": code, "status": "empty", "rows": 0}
        stale = int(getattr(df, "attrs", {}).get("stale_cache_days") or 0)
        return {
            "code": code,
            "status": "stale" if stale else "ok",
            "rows": int(len(df)),
            "latest": str(df["date"].max()) if "date" in df.columns else "",
            "stale_days": stale,
        }
    except Exception as exc:
        return {"code": code, "status": "error", "error": str(exc)[:160], "rows": 0}


def sync_research_universe(batch_size: int = None, workers: int = None,
                           history_days: int = None, path: str = None) -> Dict:
    """同步研究池的一小批股票，采用轮转游标，适合盘后定时长期运行。"""
    payload = _load_universe(path)
    if not payload.get("codes"):
        payload = refresh_research_universe(path=path)
    codes = list(payload.get("codes") or [])
    if not codes:
        return {"status": "no_universe", "synced": 0}

    batch_size = max(1, min(int(batch_size or RESEARCH_SYNC_BATCH), len(codes)))
    workers = max(1, min(int(workers or RESEARCH_SYNC_WORKERS), 2))
    history_days = max(365, int(history_days or RESEARCH_HISTORY_DAYS))
    cursor = int(payload.get("cursor") or 0) % len(codes)
    batch = [codes[(cursor + index) % len(codes)] for index in range(batch_size)]
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=history_days)).strftime("%Y-%m-%d")

    results = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="research-kline") as executor:
        futures = [executor.submit(_sync_one, item, start_date, end_date) for item in batch]
        for future in as_completed(futures):
            results.append(future.result())

    payload["cursor"] = (cursor + batch_size) % len(codes)
    summary = {
        "at": datetime.now().isoformat(),
        "requested": batch_size,
        "ok": sum(1 for row in results if row.get("status") == "ok"),
        "stale": sum(1 for row in results if row.get("status") == "stale"),
        "empty": sum(1 for row in results if row.get("status") == "empty"),
        "error": sum(1 for row in results if row.get("status") == "error"),
        "start_date": start_date,
        "end_date": end_date,
        "results": results,
    }
    payload["last_sync"] = summary
    _save_universe(payload, path)
    logger.info("研究数据同步: requested=%s ok=%s stale=%s empty=%s error=%s", summary["requested"], summary["ok"], summary["stale"], summary["empty"], summary["error"])
    return summary
