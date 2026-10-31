"""风格池与因子分的盘后影子运行、记录与评估（不下单、不写交易主库）。

每个交易日收盘后（复权日线同步完成后）运行：
1. 读取近 700 自然日的等比复权宽表，计算因子与可交易股票池；
2. 用截至当日“标签已完全可见”的窗口滚动计算因子 IC 权重，生成全池因子分；
3. 拉取估值快照与（按预算轮转的）财务指标，生成各风格候选池；
4. 计算沪深300 双通道仓位上限（影子）；
5. 全部写入 ``data/style_shadow.db``。

评估时按“t 日收盘信号、t+1 开盘买入、持有 h 日后开盘卖出、扣双边成本”计算，
并与同日可交易池等权、以及现有选股器候选（candidate_outcomes）对比。
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from config import DATA_DIR
from strategy import style_pools as sp

logger = logging.getLogger("strategy.style_shadow")

STYLE_SHADOW_DB = os.environ.get("STYLE_SHADOW_DB", os.path.join(DATA_DIR, "style_shadow.db"))
PANEL_CALENDAR_DAYS = 700
ROUND_TRIP_COST = 0.0003 * 2 + 0.0005 + 0.0005 * 2  # 佣金双边 + 印花税 + 滑点双边
INDICATOR_BUDGET = int(os.environ.get("STYLE_INDICATOR_BUDGET", "400"))
INDICATOR_STALE_DAYS = 120
PROMOTION_MIN_SIGNAL_DAYS = 20
PROMOTION_MIN_EXCESS_5D = 0.003

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS runs (date TEXT PRIMARY KEY, created_at TEXT, summary TEXT)""",
    """CREATE TABLE IF NOT EXISTS style_candidates (date TEXT, style TEXT, code TEXT, rank INTEGER,
        score REAL, detail TEXT, PRIMARY KEY (date, style, code)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS factor_weights (date TEXT, factor TEXT, weight REAL, ic_mean REAL,
        ic_ir REAL, sign_stability REAL, samples INTEGER, PRIMARY KEY (date, factor)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS factor_scores (date TEXT, code TEXT, score REAL,
        PRIMARY KEY (date, code)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS market_exposure (date TEXT PRIMARY KEY, cap REAL, long_on INTEGER,
        short_on INTEGER, close REAL, source TEXT)""",
    """CREATE TABLE IF NOT EXISTS valuations (date TEXT, code TEXT, name TEXT, pe_ttm REAL, pb_mrq REAL,
        ps_ttm REAL, pcf_ttm REAL, PRIMARY KEY (date, code)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS fundamentals (code TEXT PRIMARY KEY, report TEXT, fetched_at TEXT,
        roe REAL, np_yoy REAL, rev_yoy REAL, gross_margin REAL, debt_ratio REAL, cash_content REAL)""",
)

INDICATOR_FIELDS = {
    "index_weighted_avg_roe": "roe",
    "calculate_parent_holder_net_profit_yoy_growth_ratio": "np_yoy",
    "calculate_operating_income_yoy_growth_ratio": "rev_yoy",
    "sale_gross_margin": "gross_margin",
    "assets_debt_ratio": "debt_ratio",
    "net_profit_cash_content": "cash_content",
}


def connect(db_path: str = None, readonly: bool = False) -> sqlite3.Connection:
    path = os.path.abspath(db_path or STYLE_SHADOW_DB)
    if readonly:
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    for statement in SCHEMA:
        conn.execute(statement)
    return conn


# ── 外部快照（估值、财务指标） ─────────────────────────────────────


def latest_report(today: datetime) -> str:
    """按披露截止日推断最新可得财报期：4/30 年报+一季报，8/31 中报，10/31 三季报。"""
    y, m = today.year, today.month
    if m >= 11:
        return f"{y}-3"
    if m >= 9:
        return f"{y}-2"
    if m >= 5:
        return f"{y}-1"
    return f"{y - 1}-4"


def _previous_report(report: str) -> str:
    year, quarter = (int(part) for part in report.split("-"))
    return f"{year - 1}-4" if quarter == 1 else f"{year}-{quarter - 1}"


def _float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def refresh_valuations(conn, client, codes: List[str], date: str) -> int:
    rows = []
    for offset in range(0, len(codes), 100):
        batch = codes[offset:offset + 100]
        try:
            items = client.get_valuations(batch).get("item") or []
        except Exception as exc:  # noqa: BLE001 - 估值缺失时风格池按无估值降级
            logger.warning("估值快照获取失败(%s只): %s", len(batch), type(exc).__name__)
            continue
        for item in items:
            code = str(item.get("ticker") or str(item.get("thscode") or "")[:6])
            rows.append((date, code, str(item.get("name") or ""), _float(item.get("pe_ttm")),
                         _float(item.get("pb_mrq")), _float(item.get("ps_ttm")), _float(item.get("pcf_ttm"))))
    conn.executemany("INSERT OR REPLACE INTO valuations VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def _parse_indicators(payload) -> Dict[str, Optional[float]]:
    out = {}
    for ability in (payload or {}).get("abilities") or []:
        for indicator in ability.get("indicators") or []:
            name = INDICATOR_FIELDS.get(indicator.get("index_id"))
            if name:
                out[name] = _float(indicator.get("value"))
    return out


def refresh_fundamentals(conn, client, codes: List[str], today: datetime, budget: int = INDICATOR_BUDGET) -> int:
    """按预算轮转刷新财务指标：先补缺失，再补报告期落后或超过 120 天未刷新的。"""
    target = latest_report(today)
    known = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT code, report, fetched_at FROM fundamentals")}
    stale_before = (today - timedelta(days=INDICATOR_STALE_DAYS)).isoformat()

    def priority(code):
        if code not in known:
            return 0
        report, fetched = known[code]
        if report != target:
            return 1 if (fetched or "") < today.strftime("%Y-%m-%d") else 3
        return 2 if (fetched or "") < stale_before else 9

    queue = sorted((code for code in codes if priority(code) < 9), key=priority)[:max(0, budget)]
    done = 0
    for code in queue:
        values = {}
        report = target
        for candidate in (target, _previous_report(target)):
            try:
                values = _parse_indicators(client.get_indicators(code, candidate))
            except Exception:  # noqa: BLE001 - 单只失败不影响其他
                values = {}
            if values.get("roe") is not None:
                report = candidate
                break
        if not values:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO fundamentals VALUES (?,?,?,?,?,?,?,?,?)",
            (code, report, today.isoformat(timespec="seconds"), *(values.get(k) for k in
             ("roe", "np_yoy", "rev_yoy", "gross_margin", "debt_ratio", "cash_content"))))
        done += 1
        if done % 50 == 0:
            conn.commit()
    return done


# ── 大盘仓位上限（影子） ─────────────────────────────────────────


def market_exposure(main_db: str = None) -> Optional[dict]:
    """沪深300 双通道仓位上限；读取主库 k_daily 的指数日线（只读）。"""
    from strategy.trend_channel import dtc_states
    path = main_db or os.path.join(DATA_DIR, "quant.db")
    try:
        with closing(sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True, timeout=30)) as conn:
            frame = pd.read_sql_query(
                "SELECT date, high, low, close FROM k_daily WHERE code='000300' AND source='tencent_index' ORDER BY date",
                conn)
    except (sqlite3.Error, pd.errors.DatabaseError):
        return None
    if len(frame) < 200:
        return None
    frame = frame.set_index("date").astype(float)
    long_on, short_on = dtc_states(frame["high"], frame["low"], frame["close"])
    cap = 1.0 if (long_on.iloc[-1] and short_on.iloc[-1]) else (0.3 if long_on.iloc[-1] else 0.0)
    return {"date": frame.index[-1], "cap": cap, "long_on": int(long_on.iloc[-1]),
            "short_on": int(short_on.iloc[-1]), "close": float(frame["close"].iloc[-1]), "source": "k_daily:000300"}


# ── 每日运行 ───────────────────────────────────────────────────────


def _load_snapshot(conn, date: str):
    valuations = pd.read_sql_query(
        "SELECT code, name, pe_ttm, pb_mrq FROM valuations WHERE date=(SELECT MAX(date) FROM valuations WHERE date<=?)",
        conn, params=(date,)).set_index("code")
    fundamentals = pd.read_sql_query("SELECT * FROM fundamentals", conn).set_index("code")
    return valuations, fundamentals


def run_daily(*, date: str = None, client=None, adjusted_db: str = None, db_path: str = None,
              main_db: str = None, fetch_external: bool = True, panel: Dict[str, pd.DataFrame] = None,
              now: datetime = None) -> dict:
    """计算并记录某个收盘日的风格池、因子权重/分数与大盘仓位上限。"""
    from data import adjusted_daily
    now = now or datetime.now()
    if panel is None:
        start = (now - timedelta(days=PANEL_CALENDAR_DAYS)).strftime("%Y-%m-%d")
        panel = adjusted_daily.load_panel(start, date, db_path=adjusted_db)
    if not panel:
        return {"status": "unavailable", "reason": "复权日线库为空，请先运行 --adjusted-daily-sync"}
    factors = sp.compute_factors(panel)
    universe = sp.universe_mask(panel, factors)
    dates = list(panel["close"].index)
    asof = date or dates[-1]
    if asof not in dates:
        return {"status": "unavailable", "reason": f"复权日线缺少 {asof}"}
    pos = dates.index(asof)
    codes = sorted(universe.columns[universe.loc[asof].fillna(False).astype(bool)])

    with closing(connect(db_path)) as conn:
        external = {"valuations": 0, "fundamentals": 0}
        if fetch_external and client is not None and getattr(client, "available", False):
            external["valuations"] = refresh_valuations(conn, client, codes, asof)
            conn.commit()
            external["fundamentals"] = refresh_fundamentals(conn, client, codes, now)
            conn.commit()
        valuations, fundamentals = _load_snapshot(conn, asof)
        if not valuations.empty:
            # 名称含 ST/退 的从当日股票池剔除（历史 ST 状态不可得，仅对有快照的日期生效）
            bad = valuations.index[valuations["name"].fillna("").str.upper().str.contains("ST|退", regex=True)]
            universe.loc[asof, universe.columns.intersection(bad)] = False
            codes = [code for code in codes if code not in set(bad)]

        weights = sp.ic_weights(factors, universe, sp.forward_returns(panel, sp.ICConfig().horizon), pos)
        scores = sp.factor_scores(factors, universe, asof, weights)
        pools = sp.select_pools(factors, universe, asof, valuations=valuations,
                                fundamentals=fundamentals if not fundamentals.empty else None, scores=scores)
        exposure = market_exposure(main_db)

        conn.execute("DELETE FROM style_candidates WHERE date=?", (asof,))
        for style, frame in pools.items():
            extra = [c for c in frame.columns if c not in ("code", "rank", "score")]
            conn.executemany("INSERT INTO style_candidates VALUES (?,?,?,?,?,?)", [
                (asof, style, row["code"], int(row["rank"]), float(row["score"]),
                 json.dumps({c: (None if pd.isna(row[c]) else round(float(row[c]), 6)) for c in extra}))
                for _, row in frame.iterrows()])
        conn.executemany("INSERT OR REPLACE INTO factor_weights VALUES (?,?,?,?,?,?,?)", [
            (asof, name, stat.get("weight"), stat.get("ic_mean"), stat.get("ic_ir"),
             stat.get("sign_stability"), stat.get("samples")) for name, stat in weights.items()])
        conn.execute("DELETE FROM factor_scores WHERE date=?", (asof,))
        conn.executemany("INSERT INTO factor_scores VALUES (?,?,?)",
                         [(asof, code, float(value)) for code, value in scores.items()])
        if exposure:
            conn.execute("INSERT OR REPLACE INTO market_exposure VALUES (?,?,?,?,?,?)",
                         (exposure["date"], exposure["cap"], exposure["long_on"], exposure["short_on"],
                          exposure["close"], exposure["source"]))
        summary = {
            "status": "ok", "date": asof, "universe": len(codes),
            "pools": {style: int(len(frame)) for style, frame in pools.items()},
            "active_factors": {k: round(v["weight"], 3) for k, v in weights.items() if v.get("weight")},
            "market_exposure_cap": exposure["cap"] if exposure else None, "external": external,
        }
        conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)",
                     (asof, now.isoformat(timespec="seconds"), json.dumps(summary, ensure_ascii=False)))
        conn.commit()
    return summary


# ── 评估与晋级门槛 ─────────────────────────────────────────────────


def _legacy_candidates(main_db: str, since: str) -> pd.DataFrame:
    try:
        with closing(sqlite3.connect(f"file:{os.path.abspath(main_db)}?mode=ro", uri=True, timeout=30)) as conn:
            frame = pd.read_sql_query(
                "SELECT observation_date AS date, code FROM candidate_outcomes WHERE observation_date>=? "
                "GROUP BY observation_date, code", conn, params=(since,))
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame(columns=["date", "code"])
    frame["style"] = "legacy_picker"
    return frame


def evaluate(*, db_path: str = None, adjusted_db: str = None, main_db: str = None,
             horizons=(5, 20), since: str = None, panel=None) -> dict:
    """按风格汇总已成熟样本的扣费收益，并与等权股票池、现有选股器对比。"""
    from data import adjusted_daily
    with closing(connect(db_path, readonly=True)) as conn:
        picks = pd.read_sql_query("SELECT date, style, code FROM style_candidates"
                                  + (" WHERE date>=?" if since else ""), conn,
                                  params=(since,) if since else ())
    if picks.empty:
        return {"status": "empty", "styles": {}}
    first = picks["date"].min()
    main_db = main_db or os.path.join(DATA_DIR, "quant.db")
    picks = pd.concat([picks, _legacy_candidates(main_db, first)], ignore_index=True)
    if panel is None:
        start = (datetime.strptime(first, "%Y-%m-%d") - timedelta(days=400)).strftime("%Y-%m-%d")
        panel = adjusted_daily.load_panel(start, db_path=adjusted_db)
    factors = sp.compute_factors(panel)
    universe = sp.universe_mask(panel, factors)
    result = {"status": "ok", "styles": {}, "horizons": list(horizons)}
    for h in horizons:
        fwd = sp.forward_returns(panel, h) - ROUND_TRIP_COST
        bench = fwd.where(universe).mean(axis=1)
        stacked = fwd.stack().rename("ret").reset_index()
        stacked.columns = ["date", "code", "ret"]
        merged = picks.merge(stacked, on=["date", "code"], how="inner").dropna(subset=["ret"])
        merged["excess"] = merged["ret"] - merged["date"].map(bench)
        for style, group in merged.groupby("style"):
            daily = group.groupby("date")[["ret", "excess"]].mean()
            info = result["styles"].setdefault(style, {})
            info[f"{h}d"] = {
                "signal_days": int(len(daily)), "picks": int(len(group)),
                "mean_net": float(daily["ret"].mean()), "mean_excess": float(daily["excess"].mean()),
                "win_rate": float((group["ret"] > 0).mean()),
                "days_beating_universe": float((daily["excess"] > 0).mean()),
            }
    for style, info in result["styles"].items():
        five = info.get("5d") or {}
        legacy = (result["styles"].get("legacy_picker") or {}).get("5d") or {}
        info["promotion_ready"] = bool(
            style not in ("legacy_picker", "trend_strength")
            and five.get("signal_days", 0) >= PROMOTION_MIN_SIGNAL_DAYS
            and five.get("mean_excess", -1) >= PROMOTION_MIN_EXCESS_5D
            and five.get("mean_net", -1) > legacy.get("mean_net", -1)
        )
    return result


def format_report(report: dict) -> str:
    if report.get("status") != "ok":
        return "风格影子暂无可评估样本"
    lines = ["风格池影子评估（t+1开盘买入，持有h日后开盘卖出，已扣双边成本）"]
    for style, info in sorted(report["styles"].items()):
        label = sp.STYLE_LABELS.get(style, "现有选股器" if style == "legacy_picker" else style)
        parts = []
        for h in report["horizons"]:
            m = info.get(f"{h}d")
            if m:
                parts.append(f"{h}日: 信号{m['signal_days']}天/{m['picks']}只 净{m['mean_net']*100:+.2f}% "
                             f"超额{m['mean_excess']*100:+.2f}% 胜率{m['win_rate']:.0%}")
        ready = " ✅达到晋级门槛(需人工确认)" if info.get("promotion_ready") else ""
        lines.append(f"- {label}: " + " | ".join(parts) + ready)
    lines.append(f"晋级门槛: 5日成熟信号≥{PROMOTION_MIN_SIGNAL_DAYS}天、相对等权超额≥{PROMOTION_MIN_EXCESS_5D:.1%}、"
                 "且净收益优于现有选股器；达到后仍需人工确认才切换。")
    return "\n".join(lines)
