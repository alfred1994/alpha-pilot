"""盘中候选来源：读取上一收盘日的风格池与横截面因子分（CANDIDATE_SOURCE=style_pools）。

风格池与因子分由 ``strategy.style_shadow.run_daily`` 每个交易日收盘后写入
``data/style_shadow.db``。这里只读，不改写影子库；回测依据见
``docs/signal-drought-audit-2026-10-29.md`` §9 与 ``docs/scoring-refactor-2026-11.md``。

准入规则（替代旧的技术分 ≥58 与综合分 ≥ min_score）：
1. 必须属于 STYLE_LIVE_STYLES 中的至少一个风格池；
2. 因子分（0-100 横截面百分位）≥ STYLE_MIN_FACTOR_SCORE（默认 0，即只看入池）；
3. 按风格名额（data/style_allocation.json）加权轮转、风格内按入池排名，取前 max_reviews 只进入 LLM 否决复核。
因子分只用收盘数据，盘中不变，因此不再做“连续两轮过线”的稳定性确认。
"""
import json
import logging
import os
import sqlite3
from contextlib import closing
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("strategy.style_candidates")


def _db_path(db_path: str = None) -> str:
    from strategy.style_shadow import STYLE_SHADOW_DB
    return os.path.abspath(db_path or STYLE_SHADOW_DB)


def _expected_pool_date(plan_date: str) -> Optional[str]:
    """盘中扫描应使用上一交易日收盘生成的风格池。"""
    try:
        from scheduler.market_calendar import prev_trading_day
        value = prev_trading_day(plan_date)
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    except Exception as exc:  # noqa: BLE001 - 日历不可用时拒绝使用任何池
        logger.warning("交易日历不可用，无法校验风格池日期: %s", exc)
        return None


def load_style_candidates(plan_date: str, *, styles=None, db_path: str = None,
                          expected_date: str = None) -> Tuple[List[dict], Dict]:
    """读取上一交易日风格池候选，返回 (候选列表, 元数据)。

    风格池日期必须等于上一交易日；否则返回空列表并在元数据中说明原因，
    宁可本轮不开仓，也不用过期的池子交易。
    """
    from config import STYLE_LIVE_STYLES
    styles = tuple(styles or STYLE_LIVE_STYLES)
    expected = expected_date or _expected_pool_date(plan_date)
    meta = {"source": "style_pools", "fresh": False, "version": "", "generated_at": "",
            "candidate_count": 0, "refresh_attempted": False, "refresh_error": "",
            "styles": list(styles), "pool_date": None, "expected_pool_date": expected,
            "market_exposure_cap": None}
    path = _db_path(db_path)
    if not os.path.exists(path):
        meta["refresh_error"] = "style_shadow_db_missing"
        return [], meta
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)) as conn:
            row = conn.execute("SELECT MAX(date) FROM style_candidates WHERE date < ?", (plan_date,)).fetchone()
            pool_date = row[0] if row else None
            meta["pool_date"] = pool_date
            if not pool_date:
                meta["refresh_error"] = "no_style_pool"
                return [], meta
            run = conn.execute("SELECT created_at FROM runs WHERE date=?", (pool_date,)).fetchone()
            meta["generated_at"] = run[0] if run else ""
            meta["version"] = f"style-{pool_date}"
            exposure = conn.execute(
                "SELECT cap FROM market_exposure WHERE date<=? ORDER BY date DESC LIMIT 1", (pool_date,)).fetchone()
            meta["market_exposure_cap"] = float(exposure[0]) if exposure and exposure[0] is not None else None
            if expected is None or pool_date != expected:
                meta["refresh_error"] = f"stale_style_pool({pool_date}≠{expected})"
                return [], meta
            placeholders = ",".join("?" * len(styles))
            rows = conn.execute(
                f"SELECT code, style, rank, score, detail FROM style_candidates "
                f"WHERE date=? AND style IN ({placeholders}) ORDER BY style, rank",
                (pool_date, *styles)).fetchall()
            scores = dict(conn.execute("SELECT code, score FROM factor_scores WHERE date=?", (pool_date,)).fetchall())
            names = dict(conn.execute(
                "SELECT code, name FROM valuations WHERE date=(SELECT MAX(date) FROM valuations WHERE date<=?)",
                (pool_date,)).fetchall())
    except sqlite3.Error as exc:
        meta["refresh_error"] = f"style_db_error:{type(exc).__name__}"
        return [], meta

    merged: Dict[str, dict] = {}
    for code, style, rank, pool_score, detail in rows:
        code = str(code)
        name = str(names.get(code) or code)
        if "ST" in name.upper() or "退" in name:
            continue
        item = merged.setdefault(code, {
            "code": code, "name": name, "styles": [], "pool_ranks": {},
            "factor_score": float(scores[code]) if scores.get(code) is not None else None,
            "detail": {},
        })
        item["styles"].append(style)
        item["pool_ranks"][style] = int(rank)
        try:
            item["detail"][style] = json.loads(detail or "{}")
        except ValueError:
            item["detail"][style] = {}
    items = sorted(merged.values(), key=lambda x: (-(x["factor_score"] if x["factor_score"] is not None else -1), x["code"]))
    meta.update(fresh=True, candidate_count=len(items))
    return items, meta


def to_scored(items: List[dict]) -> List[dict]:
    """转成快链路 raw_scores 结构；composite 即因子分，旧六维分不再参与。"""
    scored = []
    for item in items:
        factor = item.get("factor_score")
        scored.append({
            "code": item["code"],
            "name": item["name"],
            "composite": round(float(factor), 1) if factor is not None else 0.0,
            "factor_score": factor,
            "styles": list(item.get("styles") or []),
            "pool_ranks": dict(item.get("pool_ranks") or {}),
            "dimensions": {"factor": {
                "score": round(float(factor), 1) if factor is not None else 0.0,
                "confidence": 1.0 if factor is not None else 0.0,
                "detail": "风格池: " + ",".join(item.get("styles") or []),
            }},
            "top_signal": "style=" + ",".join(item.get("styles") or []),
            "avg_confidence": 1.0 if factor is not None else 0.0,
            "latest_price": 0,
        })
    return scored


def select_style_candidates(scored: List[dict], *, min_factor_score: float, max_reviews: int,
                            quotas: Dict[str, int] = None) -> Tuple[List[dict], Dict[str, str]]:
    """按风格名额加权轮转选出送否决复核的候选；返回 (入选, 拒绝原因)。

    每个风格内部按入池排名（pool_ranks，缺失时按因子分）排序；风格之间按
    ``已选数/名额`` 最小者优先，某风格候选用完后名额自动让给其他风格。
    不再全局按因子分排序——factor_top 的因子分天然最高，全局排序会让其他风格永远轮不到。
    ``quotas`` 为空时各风格等权；名额为 0 或不在 quotas 中的风格不参与。
    入选项会写入 ``selected_style``，用于成交归因。
    """
    selected, reasons, queues = [], {}, {}
    for item in scored:
        factor = item.get("factor_score")
        if factor is None:
            reasons[item["code"]] = "HOLD_FACTOR_LOW(无因子分)"
            continue
        if factor < min_factor_score:
            reasons[item["code"]] = f"HOLD_FACTOR_LOW({factor:.0f}<{min_factor_score:.0f})"
            continue
        for style in item.get("styles") or []:
            queues.setdefault(style, []).append(item)
    weights = {s: (int(quotas.get(s, 0)) if quotas is not None else 1) for s in queues}
    for style, queue in queues.items():
        queue.sort(key=lambda x: ((x.get("pool_ranks") or {}).get(style, 10 ** 6), -x["factor_score"], x["code"]))
    picked = {s: 0 for s in queues}
    chosen = set()
    while len(selected) < max_reviews:
        for queue in queues.values():
            while queue and queue[0]["code"] in chosen:
                queue.pop(0)
        live = [s for s in queues if weights[s] > 0 and queues[s]]
        if not live:
            break
        style = min(live, key=lambda s: (picked[s] / weights[s], -weights[s], s))
        item = queues[style].pop(0)
        item["selected_style"] = style
        picked[style] += 1
        chosen.add(item["code"])
        selected.append(item)
    for item in scored:
        code = item["code"]
        if code not in chosen and code not in reasons:
            reasons[code] = f"HOLD_NOT_TOP{max_reviews}(factor={item['factor_score']:.0f})"
    return selected, reasons


def limit_up_price(code: str, prev_close: float) -> Optional[float]:
    """按板块涨跌幅计算涨停价（主板 10%，创业板 20%）；无昨收返回 None。"""
    try:
        prev = float(prev_close)
    except (TypeError, ValueError):
        return None
    if prev <= 0:
        return None
    pct = 0.2 if str(code).startswith("30") else 0.1
    return round(prev * (1 + pct) + 1e-9, 2)


def at_limit_up(code: str, price: float, prev_close: float) -> bool:
    limit = limit_up_price(code, prev_close)
    try:
        return limit is not None and float(price) >= limit - 0.005
    except (TypeError, ValueError):
        return False
