"""Read-only research views over persisted evidence; never fetch or evaluate on GET."""
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from scheduler.market_calendar import _now_bj
from web.public_safety import public_error_message, sanitize_public_text

router = APIRouter()
LAYERS = {
    "score_gate": "评分不足", "ranking_gate": "判断名额限制",
    "llm": "模型判断阶段", "buy_budget": "买入预算不足",
    "hold_unknown": "观望原因未分类", "legacy_unknown": "历史原因未知",
    "": "未记录拒绝层",
}
DIMENSIONS = ("technical", "capital", "sentiment", "emotion", "fundamental", "ml")
MARKET_FIELDS = {
    "hs300_pct_5d": ("沪深300近5日", "%"),
    "hs300_pct_20d": ("沪深300近20日", "%"),
    "limit_up_count": ("涨停数量", "只"),
    "limit_down_count": ("跌停数量", "只"),
    "break_rate": ("炸板率", "%"),
    "max_height": ("最高连板", "板"),
}


@contextmanager
def _open_db():
    from data.database import DB_PATH
    path = Path(DB_PATH).resolve()
    if not path.is_file():
        yield None
        return
    # Bypass Database.__enter__: dashboard reads must not initialize or migrate DBs.
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _object(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _table(conn, name):
    return conn is not None and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


@router.get("/research/market")
def market_evidence():
    """Only expose whitelisted persisted indicators, not model reasoning."""
    try:
        today = _now_bj().date()
        with _open_db() as conn:
            row = conn.execute(
                "SELECT date, regime, confidence, indicators FROM market_regimes "
                "WHERE date <= ? ORDER BY date DESC LIMIT 1", (today.isoformat(),)
            ).fetchone() if _table(conn, "market_regimes") else None
        if row is None:
            return {"success": True, "available": False, "market": None}
        indicators = _object(row["indicators"])
        trend_stale = (_number(indicators.get("trend_data_stale_days")) or 0) > 0
        metrics = []
        for key, (label, unit) in MARKET_FIELDS.items():
            value = _number(indicators.get(key))
            if key.startswith("hs300_") and trend_stale:
                value = None
            if key in ("limit_up_count", "limit_down_count", "max_height") and value is not None:
                if value < 0 or not value.is_integer():
                    value = None
            metrics.append({"key": key, "label": label, "unit": unit, "value": value})
        return {"success": True, "available": True, "market": {
            "date": row["date"], "regime": sanitize_public_text(row["regime"], 24),
            "confidence": _number(row["confidence"]), "source": "market_regimes",
            "stale": date.fromisoformat(row["date"]) < today - timedelta(days=1),
            "trend_stale": trend_stale, "metrics": metrics,
            "note": "已落库的市场识别证据，非实时全市场行情；日期新不保证各数据源新鲜。未记录题材热度和全市场涨跌分布。",
        }}
    except Exception:
        return {"success": False, "error": public_error_message()}


def _public_candidate(row):
    raw = dict(row)
    dimensions = _object(raw.get("dimensions"))
    evidence = {}
    for key in DIMENSIONS:
        item = dimensions.get(key)
        if isinstance(item, dict):
            evidence[key] = {field: _number(item.get(field)) for field in ("score", "confidence")}
    output = {key: sanitize_public_text(raw.get(key), size) for key, size in (
        ("code", 16), ("name", 40), ("scan_id", 80), ("observed_at", 32),
        ("observation_date", 16), ("hold_reason", 300), ("strategy_version", 80),
        ("price_source", 40),
    )}
    output.update({"id": raw["id"], "score": _number(raw.get("score")),
                   "dimensions": evidence,
                   "action": raw.get("action") if raw.get("action") in ("BUY", "SELL", "HOLD") else "UNKNOWN",
                   "llm_action": raw.get("llm_action") if raw.get("llm_action") in ("BUY", "SELL", "HOLD") else None,
                   "denial_label": LAYERS.get(raw.get("denial_layer"), "未分类"),
                   "fee_rate": _number(raw.get("fee_rate")),
                   "slippage_rate": _number(raw.get("slippage_rate")),
                   "net_return_3d": _number(raw.get("net_return_3d")),
                   "net_return_5d": _number(raw.get("net_return_5d")),
                   "evaluated_at": sanitize_public_text(raw.get("evaluated_at"), 32)})
    return output


@router.get("/research/candidates")
def candidate_evidence(start_date: Optional[date] = None, end_date: Optional[date] = None,
                       layer: Optional[str] = None, page: int = Query(1, ge=1, le=100000),
                       limit: int = Query(20, ge=1, le=50)):
    end = end_date or _now_bj().date()
    start = start_date or end - timedelta(days=29)
    if start > end or (end - start).days > 366:
        raise HTTPException(422, "日期范围须按先后顺序且不超过367天")
    if layer is not None and layer not in LAYERS:
        raise HTTPException(422, "未知的筛选阶段")
    where = "observation_date >= ? AND observation_date <= ?"
    params = [start.isoformat(), end.isoformat()]
    if layer is not None:
        where += " AND COALESCE(denial_layer, '') = ?"
        params.append(layer)
    try:
        with _open_db() as conn:
            if not _table(conn, "candidate_outcomes"):
                return {"success": True, "available": False}
            # Aggregates cover the complete filter, not just the visible page.
            summary = dict(conn.execute(f"""
                SELECT COUNT(*) AS observations, COUNT(DISTINCT code) AS unique_stocks,
                       COUNT(DISTINCT scan_id) AS scans,
                       SUM(CASE WHEN llm_action IN ('BUY','SELL','HOLD') THEN 1 ELSE 0 END) AS evaluated,
                       COUNT(net_return_3d) AS matured_3d, COUNT(net_return_5d) AS matured_5d,
                       COUNT(DISTINCT CASE WHEN net_return_5d IS NOT NULL THEN code END) AS matured_stocks,
                       MAX(observed_at) AS latest_observation
                FROM candidate_outcomes WHERE {where}
            """, params).fetchone())
            summary["evaluated"] = summary["evaluated"] or 0
            summary["latest_observation"] = sanitize_public_text(summary["latest_observation"], 32)
            groups = []
            for row in conn.execute(f"""
                SELECT COALESCE(denial_layer, '') AS layer, COUNT(*) AS observations,
                       COUNT(DISTINCT code) AS unique_stocks, COUNT(net_return_5d) AS matured_5d,
                       AVG(net_return_5d) AS mean_net_5d,
                       SUM(CASE WHEN net_return_5d > 0 THEN 1 ELSE 0 END) AS positive_5d
                FROM candidate_outcomes WHERE {where} GROUP BY COALESCE(denial_layer, '')
                ORDER BY COUNT(*) DESC
            """, params):
                groups.append({"label": LAYERS.get(row["layer"], "未分类"),
                               **{key: row[key] for key in ("observations", "unique_stocks", "matured_5d")},
                               "mean_net_5d": _number(row["mean_net_5d"]),
                               "positive_rate_5d": row["positive_5d"] / row["matured_5d"] if row["matured_5d"] else None})
            rows = conn.execute(f"SELECT * FROM candidate_outcomes WHERE {where} "
                                "ORDER BY observed_at DESC, id DESC LIMIT ? OFFSET ?",
                                params + [limit, (page - 1) * limit]).fetchall()
        return {"success": True, "available": True, "summary": summary, "groups": groups,
                "candidates": [_public_candidate(row) for row in rows],
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "page": page, "has_more": page * limit < summary["observations"],
                "note": "仅含已保存有效价格的扫描样本，不是全市场覆盖。按扫描观察等权统计，重复股票并非独立样本；没有题材/行业来源快照。T+N为后续N根已落库日线的扣费多头观察收益，非成交收益或SELL策略收益，未作基准调整。空值表示未成熟或缺少回填。"}
    except Exception:
        return {"success": False, "error": public_error_message()}
