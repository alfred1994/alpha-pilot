"""盘后研究股票池与历史K线增量同步。

该模块只补充研究数据，不生成订单、不修改仓位。它把盘中活跃的普通A股
逐步沉淀为宽股票池，供反事实学习和后续 pooled ML 使用。
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List

from config import DATA_DIR

logger = logging.getLogger("data.research_universe")

UNIVERSE_FILE = os.path.join(DATA_DIR, "research_universe.json")
RESEARCH_UNIVERSE_SIZE = int(os.environ.get("RESEARCH_UNIVERSE_SIZE", "300"))
RESEARCH_SYNC_BATCH = int(os.environ.get("RESEARCH_SYNC_BATCH", "12"))
RESEARCH_SYNC_WORKERS = int(os.environ.get("RESEARCH_SYNC_WORKERS", "1"))
RESEARCH_HISTORY_DAYS = int(os.environ.get("RESEARCH_HISTORY_DAYS", "730"))
RESEARCH_ITEM_TIMEOUT = int(os.environ.get("RESEARCH_ITEM_TIMEOUT", "95"))
RESEARCH_LOCK_FILE = os.path.join(DATA_DIR, "research_sync.lock")


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

    # 供应商临时返回异常时不能让研究池归零。优先复用当天盘中已验证的
    # 候选，再从已有K线库续跑；这些来源均只作为研究数据种子，不下单。
    if not ordered:
        try:
            from data.snapshot import get_candidate_pool_status
            snapshot = get_candidate_pool_status(max_age=24 * 3600).get("snapshot") or {}
            for item in snapshot.get("candidates") or []:
                code = str(item.get("code") or "").strip()
                if code and code not in seen:
                    seen.add(code)
                    ordered.append({"code": code, "name": item.get("name", code), "source": "candidate_pool_fallback"})
        except Exception:
            pass
    if not ordered:
        try:
            from data.database import Database
            with Database() as db:
                rows = db.conn.execute("""
                    SELECT code, MAX(date) AS latest_date, COUNT(*) AS row_count
                    FROM k_daily
                    WHERE code GLOB '[0-9]*' AND length(code)=6
                    GROUP BY code
                    ORDER BY latest_date DESC, row_count DESC
                    LIMIT ?
                """, (limit,)).fetchall()
            for row in rows:
                code = str(row["code"])
                if code not in seen:
                    seen.add(code)
                    ordered.append({"code": code, "name": code, "source": "kline_cache_fallback"})
        except Exception:
            pass
    for item in current.get("codes") or []:
        code = str(item.get("code") or "")
        if code and code not in seen and len(ordered) < limit:
            seen.add(code)
            ordered.append({"code": code, "name": item.get("name", code), "source": "retained"})

    payload = {
        "version": 1,
        "generated_at": datetime.now().isoformat(),
        "source": ordered[0].get("source", "none") if ordered else "none",
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


def _sync_worker(item: Dict, start_date: str, end_date: str, result_queue):
    """Linux研究子进程入口；单只卡住时可被父进程安全终止。"""
    try:
        result_queue.put(_sync_one(item, start_date, end_date))
    except Exception as exc:
        result_queue.put({"code": item.get("code", ""), "status": "error", "error": str(exc)[:160], "rows": 0})


def _sync_one_bounded(item: Dict, start_date: str, end_date: str) -> Dict:
    """以独立进程为单股票同步设置硬上限，盘后任务不能无限悬挂。"""
    if os.name == "nt":
        return _sync_one(item, start_date, end_date)
    import multiprocessing as mp
    import queue

    ctx = mp.get_context("fork")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_sync_worker, args=(item, start_date, end_date, result_queue))
    process.start()
    process.join(RESEARCH_ITEM_TIMEOUT)
    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(2)
        return {"code": item.get("code", ""), "status": "timeout", "rows": 0}
    try:
        return result_queue.get_nowait()
    except queue.Empty:
        return {"code": item.get("code", ""), "status": "error", "error": "worker_no_result", "rows": 0}


def _acquire_lock(path: str = None) -> bool:
    path = path or RESEARCH_LOCK_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"pid": os.getpid(), "started_at": datetime.now().isoformat()}, file)
        return True
    except FileExistsError:
        try:
            age = datetime.now().timestamp() - os.path.getmtime(path)
            if age > RESEARCH_ITEM_TIMEOUT * 2:
                os.unlink(path)
                return _acquire_lock(path)
        except OSError:
            pass
        return False


def _release_lock(path: str = None):
    try:
        os.unlink(path or RESEARCH_LOCK_FILE)
    except FileNotFoundError:
        pass


def sync_research_universe(batch_size: int = None, workers: int = None,
                           history_days: int = None, path: str = None) -> Dict:
    """同步研究池的一小批股票，采用轮转游标，适合盘后定时长期运行。"""
    lock_path = f"{path}.lock" if path else RESEARCH_LOCK_FILE
    if not _acquire_lock(lock_path):
        return {"status": "locked", "synced": 0}
    try:
        payload = _load_universe(path)
        if not payload.get("codes"):
            payload = refresh_research_universe(path=path)
        codes = list(payload.get("codes") or [])
        if not codes:
            return {"status": "no_universe", "synced": 0}

        batch_size = max(1, min(int(batch_size or RESEARCH_SYNC_BATCH), len(codes)))
        # 研究同步优先确定性和可恢复性；单标的已在子进程隔离，workers 参数
        # 预留给后续可靠数据源扩容，当前强制顺序执行避免供应商并发封锁。
        workers = 1
        history_days = max(365, int(history_days or RESEARCH_HISTORY_DAYS))
        cursor = int(payload.get("cursor") or 0) % len(codes)
        batch = [codes[(cursor + index) % len(codes)] for index in range(batch_size)]
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=history_days)).strftime("%Y-%m-%d")
        results = [_sync_one_bounded(item, start_date, end_date) for item in batch]

        payload["cursor"] = (cursor + batch_size) % len(codes)
        summary = {
            "at": datetime.now().isoformat(),
            "requested": batch_size,
            "workers": workers,
            "ok": sum(1 for row in results if row.get("status") == "ok"),
            "stale": sum(1 for row in results if row.get("status") == "stale"),
            "empty": sum(1 for row in results if row.get("status") == "empty"),
            "timeout": sum(1 for row in results if row.get("status") == "timeout"),
            "error": sum(1 for row in results if row.get("status") == "error"),
            "start_date": start_date,
            "end_date": end_date,
            "results": results,
        }
        payload["last_sync"] = summary
        _save_universe(payload, path)
        logger.info("研究数据同步: requested=%s ok=%s stale=%s empty=%s timeout=%s error=%s", summary["requested"], summary["ok"], summary["stale"], summary["empty"], summary["timeout"], summary["error"])
        return summary
    finally:
        _release_lock(lock_path)
