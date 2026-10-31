"""按比例复权的全市场日线库（研究、风格选股与打分旁路使用，不写交易主库）。

背景：主库 ``k_daily`` 的前复权是“等差”口径（价格减去累计分红），百分比类
指标（收益率、ATR%、动量、波动率）越早失真越大，例如茅台 2016 年被存为 10 元
左右、单日涨跌常出现 ±40%。这里改用同花顺(HiThink)市场数据包提供的不复权日线
与除权事件，按交易所除权参考价逐日链式计算收益率：

    参考价 = (前收 - 每股派息 + 配股价 × 配股比例) / (1 + 每股送转 + 配股比例)
    日收益 = 收盘 / 参考价 - 1

复权因子按“后复权”从每只股票的首个交易日开始累乘，因此新增数据只追加，不改写
已存历史；任意时点的等比前复权价格可由 ``收盘 × 因子`` 再整体缩放得到，比值不变。

库文件默认 ``data/adjusted_daily.db``（环境变量 ``ADJUSTED_DAILY_DB`` 可覆盖），
与交易主库分离，避免长事务锁住盘中链路。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from typing import Dict, Iterable, Optional
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from config import DATA_DIR

logger = logging.getLogger("data.adjusted_daily")

ADJUSTED_DAILY_DB = os.environ.get("ADJUSTED_DAILY_DB", os.path.join(DATA_DIR, "adjusted_daily.db"))
DUMP_DIR = os.path.join(DATA_DIR, "market_dumps")
# 沪深主板与创业板普通股；科创板、北交所无交易权限，不纳入。
ELIGIBLE_CODE = r"^(?:00[0-3]|30[01]|60[0-35])\d{3}$"
CHINEXT_20PCT_SINCE = "2020-08-24"
LIMIT_TOLERANCE = 0.012
ALLOWED_DUMP_HOST_SUFFIX = ".thsi.cn"
EVENT_COLUMNS = ["dividend", "bonus", "allot_ratio", "allot_price"]
BAR_COLUMNS = ["code", "date", "open", "high", "low", "close", "volume", "amount"]

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS bars (
        code TEXT NOT NULL, date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
        ref REAL, ret REAL, factor REAL, flag INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (code, date)) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS idx_bars_date ON bars(date)",
    """CREATE TABLE IF NOT EXISTS events (
        code TEXT NOT NULL, ex_date TEXT NOT NULL,
        dividend REAL, bonus REAL, allot_ratio REAL, allot_price REAL,
        PRIMARY KEY (code, ex_date)) WITHOUT ROWID""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
)


def connect(db_path: str = None, readonly: bool = False) -> sqlite3.Connection:
    path = os.path.abspath(db_path or ADJUSTED_DAILY_DB)
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


# ── 原始数据规范化 ─────────────────────────────────────────────────


def _shanghai_date(ms: pd.Series) -> pd.Series:
    return pd.to_datetime(ms, unit="ms", utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")


def normalize_raw(frame: pd.DataFrame) -> pd.DataFrame:
    """把同花顺日线包(thscode/date_ms/*_price/volume/turnover)转成统一列。"""
    if "thscode" in frame.columns:
        out = pd.DataFrame({
            "code": frame["thscode"].astype(str).str[:6],
            "date": _shanghai_date(frame["date_ms"]),
            "open": frame["open_price"], "high": frame["high_price"],
            "low": frame["low_price"], "close": frame["close_price"],
            "volume": frame["volume"], "amount": frame["turnover"],
        })
    else:
        out = frame[BAR_COLUMNS].copy()
        out["code"] = out["code"].astype(str).str.zfill(6)
    for column in BAR_COLUMNS[2:]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out[out["code"].str.match(ELIGIBLE_CODE)]
    out = out[(out["close"] > 0) & (out["volume"] > 0) & (out["high"] >= out["low"])]
    return out.sort_values(["code", "date"]).drop_duplicates(["code", "date"], keep="last").reset_index(drop=True)


def normalize_events(frame: pd.DataFrame) -> pd.DataFrame:
    """把同花顺除权事件包转成 (code, ex_date, dividend, bonus, allot_ratio, allot_price)。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["code", "ex_date", *EVENT_COLUMNS])
    if "ticker" in frame.columns or "thscode" in frame.columns:
        code = (frame["ticker"] if "ticker" in frame.columns else frame["thscode"]).astype(str).str[:6]
        out = pd.DataFrame({
            "code": code, "ex_date": _shanghai_date(frame["ex_date_ms"]),
            "dividend": frame["dividend_per_share"], "bonus": frame["per_share_bonus"],
            "allot_ratio": frame["allotment_ratio"], "allot_price": frame["allotment_price"],
        })
    else:
        out = frame[["code", "ex_date", *EVENT_COLUMNS]].copy()
    for column in EVENT_COLUMNS:
        out[column] = pd.to_numeric(out[column], errors="coerce").fillna(0.0)
    out = out[out["code"].str.match(ELIGIBLE_CODE)]
    agg = out.groupby(["code", "ex_date"], as_index=False).agg(
        dividend=("dividend", "sum"), bonus=("bonus", "sum"),
        allot_ratio=("allot_ratio", "sum"), allot_price=("allot_price", "max"))
    return agg.sort_values(["code", "ex_date"]).reset_index(drop=True)


def _price_limit(code: pd.Series, date: pd.Series) -> np.ndarray:
    chinext = code.str.startswith("30") & (date >= CHINEXT_20PCT_SINCE)
    return np.where(chinext, 0.2, 0.1)


# ── 核心计算 ───────────────────────────────────────────────────────


def compute_adjusted(raw: pd.DataFrame, events: pd.DataFrame,
                     base: Dict[str, tuple] = None) -> pd.DataFrame:
    """计算参考价、日收益与后复权因子。

    Args:
        raw: 规范化日线（同一股票内日期递增）
        events: 规范化除权事件
        base: {code: (last_date, last_close, last_factor)}，增量更新时把链条接到已存历史

    除权日不在交易日时（停牌期间除权），事件并入其后第一个交易日。
    超出涨跌幅限制的异常日（新股首日、数据瑕疵）收益置 0 并标记 flag=1，不制造收益。
    """
    base = base or {}
    if raw.empty:
        return raw.assign(ref=[], ret=[], factor=[], flag=[])
    frame = raw.sort_values(["code", "date"]).reset_index(drop=True).copy()
    frame["_seq"] = np.arange(len(frame))

    # 事件映射到“该股不早于除权日的首个交易日”，且必须晚于链条起点（已存历史或本批首日）
    for column in EVENT_COLUMNS:
        frame[column] = 0.0
    if events is not None and not events.empty:
        first_date = frame.groupby("code")["date"].min()
        start = {code: base.get(code, (None,))[0] or first_date.get(code) for code in first_date.index}
        ev = events[events["code"].isin(first_date.index)].copy()
        ev = ev[ev["ex_date"] > ev["code"].map(start)]
        if not ev.empty:
            ev["_dt"] = pd.to_datetime(ev["ex_date"])
            days = frame[["code", "date", "_seq"]].copy()
            days["_dt"] = pd.to_datetime(days["date"])
            mapped = pd.merge_asof(ev.sort_values("_dt"), days.sort_values("_dt"), on="_dt",
                                   by="code", direction="forward").dropna(subset=["_seq"])
            if not mapped.empty:
                mapped = mapped.groupby("_seq").agg(
                    dividend=("dividend", "sum"), bonus=("bonus", "sum"),
                    allot_ratio=("allot_ratio", "sum"), allot_price=("allot_price", "max"))
                idx = mapped.index.astype(int)
                for column in EVENT_COLUMNS:
                    frame.loc[idx, column] = mapped[column].values

    prev_close = frame.groupby("code")["close"].shift(1)
    first_rows = prev_close.isna()
    base_close = frame["code"].map({code: value[1] for code, value in base.items()})
    prev_close = prev_close.where(~first_rows, base_close)
    frame["ref"] = ((prev_close - frame["dividend"] + frame["allot_price"] * frame["allot_ratio"])
                    / (1 + frame["bonus"] + frame["allot_ratio"]))
    frame["ret"] = frame["close"] / frame["ref"] - 1
    frame["flag"] = 0
    no_chain = frame["ref"].isna() | ~(frame["ref"] > 0)
    frame.loc[no_chain, "ret"] = 0.0
    limit = _price_limit(frame["code"], frame["date"])
    bad = ~no_chain & (frame["ret"].abs() > limit + LIMIT_TOLERANCE)
    frame.loc[bad, "ret"] = 0.0
    frame.loc[bad, "flag"] = 1

    growth = (1 + frame["ret"]).groupby(frame["code"]).cumprod()
    base_adj = frame["code"].map({code: value[1] * value[2] for code, value in base.items()})
    start_adj = base_adj.where(base_adj.notna(), frame.groupby("code")["close"].transform("first"))
    adj = start_adj * growth
    # 无链条首行：因子=1（后复权起点）
    frame["factor"] = adj / frame["close"]
    return frame.drop(columns=["_seq", *EVENT_COLUMNS])


# ── 存储 ───────────────────────────────────────────────────────────


def _upsert_bars(conn, frame: pd.DataFrame, chunk: int = 200_000):
    columns = ["code", "date", "open", "high", "low", "close", "volume", "amount", "ref", "ret", "factor", "flag"]
    sql = f"INSERT OR REPLACE INTO bars ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})"
    for start in range(0, len(frame), chunk):
        part = frame.iloc[start:start + chunk][columns]
        rows = part.astype(object).where(part.notna(), None).itertuples(index=False, name=None)
        conn.executemany(sql, rows)


def _upsert_events(conn, events: pd.DataFrame):
    if events is None or events.empty:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO events (code, ex_date, dividend, bonus, allot_ratio, allot_price) VALUES (?,?,?,?,?,?)",
        events[["code", "ex_date", *EVENT_COLUMNS]].itertuples(index=False, name=None))


def _set_meta(conn, **values):
    conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     [(key, str(value)) for key, value in values.items()])


def _chain_base(conn, codes: Iterable[str], before: Dict[str, str]) -> Dict[str, tuple]:
    """取每只股票在本批首日之前的最后一根已存 K 线，作为增量链条起点。"""
    base = {}
    for code in codes:
        row = conn.execute(
            "SELECT date, close, factor FROM bars WHERE code=? AND date<? ORDER BY date DESC LIMIT 1",
            (code, before[code])).fetchone()
        if row and row[1] and row[2]:
            base[code] = (row[0], float(row[1]), float(row[2]))
    return base


def store(raw: pd.DataFrame, events: pd.DataFrame, *, db_path: str = None, full: bool = False,
          source: str = "") -> dict:
    """写入一批日线：full=True 时整库重建，否则把链条接到已存历史后覆盖本批日期。"""
    raw = normalize_raw(raw)
    events = normalize_events(events)
    with closing(connect(db_path)) as conn:
        if full:
            conn.execute("DELETE FROM bars")
            base = {}
        else:
            firsts = raw.groupby("code")["date"].min().to_dict()
            base = _chain_base(conn, firsts, firsts)
        all_events = events
        if not full:
            stored = pd.read_sql_query("SELECT code, ex_date, " + ",".join(EVENT_COLUMNS) + " FROM events", conn)
            all_events = (pd.concat([stored, events]).drop_duplicates(["code", "ex_date"], keep="last")
                          if not stored.empty else events)
        frame = compute_adjusted(raw, all_events, base=base)
        _upsert_events(conn, events)
        _upsert_bars(conn, frame)
        latest = conn.execute("SELECT MAX(date), COUNT(DISTINCT code) FROM bars").fetchone()
        summary = {
            "rows": int(len(frame)), "codes": int(frame["code"].nunique()),
            "flagged": int(frame["flag"].sum()), "max_date": latest[0], "universe_codes": latest[1],
            "mode": "full" if full else "incremental", "source": source,
        }
        _set_meta(conn, last_sync=datetime.now().isoformat(timespec="seconds"), **{
            f"last_{key}": value for key, value in summary.items()})
        conn.commit()
    return summary


# ── 同花顺数据包下载 ─────────────────────────────────────────────


def _download(url: str, path: str):
    import requests

    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(ALLOWED_DUMP_HOST_SUFFIX):
        raise ValueError("数据包下载地址不在可信域名内")
    tmp = path + ".part"
    with requests.get(url, stream=True, timeout=120, allow_redirects=False) as response:
        response.raise_for_status()
        with open(tmp, "wb") as handle:
            for chunk in response.iter_content(1 << 20):
                handle.write(chunk)
    os.replace(tmp, path)


def fetch_dumps(*, full: bool, client=None, dump_dir: str = None) -> Dict[str, str]:
    """下载日线(全量10年或近10日)与除权事件数据包，返回本地 parquet 路径。"""
    if client is None:
        from data.hithink import HiThinkClient
        client = HiThinkClient(timeout=90)
    if not client.available:
        raise RuntimeError("未配置 HITHINK_FINANCE_API_KEY，无法下载市场数据包")
    dump_dir = dump_dir or DUMP_DIR
    os.makedirs(dump_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    paths = {}
    for kind, name in ((("daily-k" if full else "daily-k-10d"), "daily"), ("adjustment-factors", "events")):
        path = os.path.join(dump_dir, f"{name}_{'full' if full and name == 'daily' else 'recent'}_{stamp}.parquet")
        meta = client.get_market_dump(kind)
        _download(str(meta.get("presigned_url") or ""), path)
        paths[name] = path
    return paths


def sync(*, full: bool = None, db_path: str = None, client=None, dump_dir: str = None,
         keep_dumps: int = 2) -> dict:
    """盘后同步入口：库为空时自动全量，否则用近10日数据包增量更新。"""
    started = time.time()
    if full is None:
        try:
            with closing(connect(db_path)) as conn:
                full = conn.execute("SELECT 1 FROM bars LIMIT 1").fetchone() is None
        except sqlite3.Error:
            full = True
    paths = fetch_dumps(full=full, client=client, dump_dir=dump_dir)
    raw = pd.read_parquet(paths["daily"])
    events = pd.read_parquet(paths["events"])
    summary = store(raw, events, db_path=db_path, full=full, source=os.path.basename(paths["daily"]))
    summary["elapsed_seconds"] = round(time.time() - started, 1)
    _prune_dumps(dump_dir or DUMP_DIR, keep_dumps)
    logger.info("复权日线同步完成: %s", summary)
    return summary


def _prune_dumps(dump_dir: str, keep: int):
    try:
        files = sorted((os.path.join(dump_dir, name) for name in os.listdir(dump_dir) if name.endswith(".parquet")),
                       key=os.path.getmtime, reverse=True)
    except OSError:
        return
    for path in files[keep * 2:]:
        try:
            os.remove(path)
        except OSError:
            pass


# ── 读取 ───────────────────────────────────────────────────────────


def status(db_path: str = None) -> dict:
    try:
        with closing(connect(db_path, readonly=True)) as conn:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            row = conn.execute("SELECT MAX(date), MIN(date) FROM bars").fetchone()
    except (FileNotFoundError, sqlite3.Error) as exc:
        return {"available": False, "reason": type(exc).__name__}
    return {"available": row[0] is not None, "max_date": row[0], "min_date": row[1], **meta}


def load_panel(start: str, end: str = None, *, codes: Optional[Iterable[str]] = None,
               db_path: str = None) -> Dict[str, pd.DataFrame]:
    """读取 [start, end] 宽表：日期 × 股票。

    返回键: close/open/high/low（等比复权，可直接算收益率与 ATR%），
    raw_close/raw_open（不复权，用于一手成本与涨跌停判断），ref, amount, volume, flag。
    """
    query = "SELECT code, date, open, high, low, close, volume, amount, ref, factor, flag FROM bars WHERE date >= ?"
    params = [start]
    if end:
        query += " AND date <= ?"
        params.append(end)
    codes = list(codes or [])
    if codes:
        query += f" AND code IN ({','.join('?' * len(codes))})"
        params.extend(codes)
    with closing(connect(db_path, readonly=True)) as conn:
        frame = pd.read_sql_query(query, conn, params=params)
    if frame.empty:
        return {}
    for column in ("open", "high", "low", "close"):
        frame[f"adj_{column}"] = frame[column] * frame["factor"]
    wide = {}
    mapping = {"close": "adj_close", "open": "adj_open", "high": "adj_high", "low": "adj_low",
               "raw_close": "close", "raw_open": "open", "ref": "ref", "amount": "amount",
               "volume": "volume", "flag": "flag"}
    for name, column in mapping.items():
        wide[name] = frame.pivot(index="date", columns="code", values=column).astype("float64")
    return wide
