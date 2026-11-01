"""体彩 NBA 纸面验证的只读接口。

注单在出单时即公开（选边、赔率、偏离、估算胜率与期望值），赛后补结果与收盘价差；
场次页同样在开赛前展示偏离与期望值。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from lottery import signals, stats
from lottery.sources import now_beijing
from lottery.teams import KEY2CN
from web.api_errors import unavailable

router = APIRouter()
DISCLAIMER = "纸面预测记录，不构成投注建议；不代购、不下单。理性购彩，未满 18 周岁禁止购彩。"
RUN_DETAIL_KEYS = ("date", "games", "with_ref", "open_games", "new_picks", "picks_today",
                   "new_full_card", "full_card_today", "games_updated", "close_lines", "settled")


@contextmanager
def _open():
    from lottery.store import LOTTERY_DB
    path = Path(LOTTERY_DB).resolve()
    if not path.is_file():
        yield None
        return
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _started(tip_at: Optional[str], now: datetime) -> bool:
    try:
        return bool(tip_at) and datetime.fromisoformat(tip_at) <= now
    except ValueError:
        return False


def _pick_view(row: dict) -> dict:
    home_cn = row.get("home_cn") or KEY2CN.get(row["home"], row["home"])
    away_cn = row.get("away_cn") or KEY2CN.get(row["away"], row["away"])
    return {
        "id": row["id"], "sale_date": row["sale_date"], "matchnum": row.get("matchnum"),
        "matchup": f"{away_cn} @ {home_cn}", "strategy": row["strategy"],
        "strategy_label": stats.STRATEGY_LABELS.get(row["strategy"], row["strategy"]),
        "market": row["market"], "selection": signals.describe(row["market"], row["side"], row["line"], home_cn, away_cn),
        "odds": row["odds"], "ref_line": row.get("ref_line"), "edge": row.get("edge"),
        "p_win": row.get("p_win"), "ev": row.get("ev"),
        "single_open": None if row.get("single_open") is None else bool(row["single_open"]),
        "created_at": row.get("created_at"), "result": row.get("result"), "pnl": row.get("pnl"),
        "score": (f"{row['away_score']}-{row['home_score']}" if row.get("home_score") is not None else None),
        "close_ref": row.get("close_ref"), "clv": row.get("clv"), "source": row.get("source"),
    }


def _all_picks(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM picks ORDER BY sale_date, id")]


@router.get("/lottery/summary")
def lottery_summary():
    now = now_beijing()
    try:
        with _open() as conn:
            if conn is None:
                return {"success": True, "available": False, "disclaimer": DISCLAIMER}
            rows = _all_picks(conn)
            last_game_day = conn.execute("SELECT MAX(sale_date) FROM games").fetchone()[0]
        strategies = []
        for key in ("sharp", "all_games", "model_legacy"):
            subset = [r for r in rows if r["strategy"] == key]
            if not subset and key != "sharp":
                continue
            by_market = {m: stats.summarize([r for r in subset if r["market"] == m]) for m in ("让分", "大小分")}
            strategies.append({"key": key, "label": stats.STRATEGY_LABELS[key],
                               "overall": stats.summarize(subset), "by_market": by_market})
        latest_rows = [r for r in rows if r["sale_date"] == last_game_day and r["strategy"] in ("sharp", "all_games")]
        return {
            "success": True, "available": True, "as_of": now.isoformat(timespec="seconds"),
            "latest_sale_date": last_game_day, "strategies": strategies,
            "latest_day_picks": {key: sum(r["strategy"] == key for r in latest_rows) for key in ("sharp", "all_games")},
            "stop_rules": stats.stop_status([r for r in rows if r["strategy"] == "sharp"]),
            "curve": stats.curve(rows),
            "thresholds": {
                "min_ev": signals.MIN_EV, "max_picks_per_day": signals.MAX_PICKS_PER_DAY,
                "required_edge_at_1_70": {m: signals.required_edge(1.70, m) for m in ("让分", "大小分")},
                "breakeven_at_1_70": round(signals.breakeven(1.70), 4),
            },
            "disclaimer": DISCLAIMER,
        }
    except Exception:
        return unavailable()


@router.get("/lottery/games")
def lottery_games(sale_date: Optional[date] = None):
    now = now_beijing()
    try:
        with _open() as conn:
            if conn is None:
                return {"success": True, "available": False, "games": [], "disclaimer": DISCLAIMER}
            day = sale_date.isoformat() if sale_date else conn.execute("SELECT MAX(sale_date) FROM games").fetchone()[0]
            if not day:
                return {"success": True, "available": False, "games": [], "disclaimer": DISCLAIMER}
            games = [dict(r) for r in conn.execute("SELECT * FROM games WHERE sale_date=? ORDER BY matchnum", (day,))]
            snaps = {}
            for row in conn.execute(
                    "SELECT * FROM snapshots WHERE sale_date=? AND source='picks' ORDER BY captured_at", (day,)):
                snaps[(row["home"], row["away"])] = dict(row)
            picks = {}
            for row in conn.execute("SELECT * FROM picks WHERE sale_date=?", (day,)):
                picks.setdefault((row["home"], row["away"]), []).append(dict(row))
        out = []
        for game in games:
            key = (game["home"], game["away"])
            snap = snaps.get(key, {})
            started = _started(game.get("tip_at"), now) or game.get("home_score") is not None
            item = {
                "matchnum": game.get("matchnum"), "home": game.get("home_cn") or KEY2CN.get(game["home"]),
                "away": game.get("away_cn") or KEY2CN.get(game["away"]), "tip_at": game.get("tip_at"),
                "state": game.get("state"), "started": started,
                "score": f"{game['away_score']}-{game['home_score']}" if game.get("home_score") is not None else None,
                "jc_line": snap.get("jc_line"), "jc_odds": [snap.get("jc_sp_home"), snap.get("jc_sp_away")],
                "jc_total": snap.get("jc_total"), "jc_total_odds": [snap.get("jc_sp_over"), snap.get("jc_sp_under")],
                "single_spread": None if snap.get("single_spread") is None else bool(snap["single_spread"]),
                "single_total": None if snap.get("single_total") is None else bool(snap["single_total"]),
                "ref_provider": snap.get("ref_provider"), "ref_line": snap.get("ref_line"), "ref_total": snap.get("ref_total"),
                "close_ref_line": game.get("close_ref_line"),
                "close_ref_total": game.get("close_ref_total"),
                "captured_at": snap.get("captured_at"),
                "analysis": None, "picks": [],
            }
            cands = signals.full_card(snap) if snap else []
            item["analysis"] = [{k: c[k] for k in ("market", "side", "edge", "odds", "p_win", "ev")} for c in cands]
            item["picks"] = [_pick_view(p) for p in sorted(picks.get(key, []), key=lambda p: (p["strategy"], p["market"]))]
            out.append(item)
        return {"success": True, "available": True, "sale_date": day, "games": out, "disclaimer": DISCLAIMER}
    except Exception:
        return unavailable()


@router.get("/lottery/ledger")
def lottery_ledger(strategy: Optional[str] = None, market: Optional[str] = None,
                   limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    if strategy and strategy not in stats.STRATEGY_LABELS:
        raise HTTPException(422, "未知策略")
    if market and market not in ("让分", "大小分"):
        raise HTTPException(422, "未知玩法")
    now = now_beijing()
    try:
        with _open() as conn:
            if conn is None:
                return {"success": True, "available": False, "items": [], "total": 0, "disclaimer": DISCLAIMER}
            rows = _all_picks(conn)
        rows = [r for r in rows if (not strategy or r["strategy"] == strategy) and (not market or r["market"] == market)]
        rows.sort(key=lambda r: (r["sale_date"], r["id"]), reverse=True)
        items = [{**_pick_view(r), "started": _started(r.get("tip_at"), now)} for r in rows[offset:offset + limit]]
        return {"success": True, "available": True, "total": len(rows), "items": items, "disclaimer": DISCLAIMER}
    except Exception:
        return unavailable()


@router.get("/lottery/health")
def lottery_health():
    try:
        with _open() as conn:
            if conn is None:
                return {"success": True, "available": False}
            jobs = {}
            for job in ("picks", "capture"):
                row = conn.execute("SELECT started_at, finished_at, status, detail FROM runs WHERE job=? ORDER BY id DESC LIMIT 1",
                                   (job,)).fetchone()
                ok = conn.execute("SELECT MAX(finished_at) FROM runs WHERE job=? AND status='ok'", (job,)).fetchone()[0]
                fails = conn.execute("SELECT COUNT(*) FROM runs WHERE job=? AND status='failed' AND started_at >= datetime('now','-7 day')",
                                     (job,)).fetchone()[0]
                detail = {}
                if row and row["detail"]:
                    try:
                        parsed = json.loads(row["detail"])
                        detail = {k: parsed[k] for k in RUN_DETAIL_KEYS if k in parsed}
                    except ValueError:
                        detail = {}
                jobs[job] = {"last_started_at": row["started_at"] if row else None,
                             "last_status": row["status"] if row else None,
                             "last_success_at": ok, "failures_7d": fails, "detail": detail}
            latest = conn.execute("SELECT MAX(captured_at) FROM snapshots").fetchone()[0]
            single = conn.execute(
                """SELECT COUNT(*), SUM(single_spread=1), SUM(single_total=1) FROM snapshots
                   WHERE source='picks' AND sale_date >= date('now','-7 day')""").fetchone()
            coverage = conn.execute(
                """SELECT COUNT(*), SUM(ref_line IS NOT NULL) FROM snapshots
                   WHERE source='picks' AND sale_date >= date('now','-7 day')""").fetchone()
        return {"success": True, "available": True, "jobs": jobs, "latest_snapshot_at": latest,
                "single_bet_7d": {"snapshots": single[0] or 0, "spread_open": single[1] or 0, "total_open": single[2] or 0},
                "reference_coverage_7d": {"snapshots": coverage[0] or 0, "with_reference": coverage[1] or 0},
                "sources": [{"name": "竞彩盘口", "detail": "500.com 竞彩篮球（官方 SP 镜像）"},
                            {"name": "国际盘口与赛果", "detail": "ESPN 计分板内嵌盘口（通常为 DraftKings）"}]}
    except Exception:
        return unavailable()
