"""技术形态的只读影子筛选；不生成 TradeOrder 或修改主交易评分。

盘中仅使用已经结束的日线。RPS 的比较集来自同日期的本地研究数据，
缺少横截面时明确报告不足，不能用单股涨幅代替相对强度。
"""
import argparse
import json
import sqlite3
from dataclasses import asdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

STRATEGIES = (
    "high_tight_flag",
    "rps_breakout",
    "uptrend_limit_down",
    "turtle_trade",
)


def completed_daily_cutoff(now=None):
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.tzinfo is not None:
        now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    day = now.date()
    if now.hour < 15:
        day -= timedelta(days=1)
    return day.isoformat()


def evaluate_patterns(code, frame, *, as_of=None, universe_closes=None):
    """保留形态条件和版本，输出只供观察的信号，不加入 dimensions。"""
    from strategy.strategies import get_strategy

    cutoff = as_of or completed_daily_cutoff()
    cutoff = pd.Timestamp(cutoff).date().isoformat()
    data = frame.copy() if frame is not None else pd.DataFrame()
    if "date" in data:
        dates = pd.to_datetime(data["date"], errors="coerce")
        data = data.loc[dates.notna() & (dates <= pd.Timestamp(cutoff))].copy()
    latest = pd.to_datetime(data["date"]).max() if "date" in data and not data.empty else pd.NaT
    lag = (pd.Timestamp(cutoff) - latest).days if pd.notna(latest) else None
    # 保守的自然日上限，不冒充交易所日历；长假期间可暂时无形态诊断。
    if lag is None or lag > 7:
        return {"mode": "shadow", "as_of": cutoff, "data_as_of": None if pd.isna(latest) else latest.date().isoformat(),
                "status": "stale" if lag is not None else "unavailable", "affects_orders": False, "signals": []}
    signals = []
    for name in STRATEGIES:
        strategy = get_strategy(name)
        signal = strategy.generate_signals(
            code, data, as_of=cutoff, universe_closes=universe_closes,
        )
        signals.append({"strategy": name, "version": strategy.version, **asdict(signal)})
    return {"mode": "shadow", "as_of": cutoff, "data_as_of": latest.date().isoformat(),
            "calendar_lag_days": lag, "status": "ok", "affects_orders": False, "signals": signals}


def screen_cached_universe(*, db_path=None, as_of=None, codes=None, limit=800):
    """以共享缓存生成可审查的横截面报告，不拉行情、不写数据库。

    比较集是当前缓存覆盖集，不宣称全市场或历史时点指数成分；历史回看
    仍可能有幸存者偏差，不能把报告当作策略收益回测。
    """
    from config import DATA_DIR

    cutoff = pd.Timestamp(as_of or completed_daily_cutoff()).date().isoformat()
    if not isinstance(limit, int) or isinstance(limit, bool) or not 20 <= limit <= 5000:
        raise ValueError("limit 必须为 20 到 5000")
    path = Path(db_path or Path(DATA_DIR) / "quant.db").resolve()
    result = {"mode": "shadow", "as_of": cutoff, "universe_source": "local_k_daily_cache",
              "survivorship_bias_possible": True, "affects_orders": False, "items": []}
    if not path.exists():
        return {**result, "status": "unavailable", "reason": "历史缓存不存在"}
    start = (pd.Timestamp(cutoff) - pd.Timedelta(days=550)).date().isoformat()
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            eligible = "(code GLOB '000[0-9][0-9][0-9]' OR code GLOB '001[0-9][0-9][0-9]' OR code GLOB '002[0-9][0-9][0-9]' OR code GLOB '003[0-9][0-9][0-9]' OR code GLOB '300[0-9][0-9][0-9]' OR code GLOB '301[0-9][0-9][0-9]' OR code GLOB '60[0-9][0-9][0-9][0-9]')"
            universe = pd.read_sql_query(
                f"SELECT code, MAX(date) latest FROM k_daily WHERE date <= ? AND date >= ? AND {eligible} GROUP BY code ORDER BY latest DESC, code LIMIT ?",
                connection, params=(cutoff, start, limit),
            )
            if universe.empty:
                return {**result, "status": "unavailable", "reason": "没有可用普通A股历史"}
            symbols = universe["code"].tolist()
            placeholders = ",".join("?" for _ in symbols)
            history = pd.read_sql_query(
                f"SELECT code,date,open,high,low,close,volume,amount FROM k_daily WHERE code IN ({placeholders}) AND date >= ? AND date <= ? ORDER BY date,code",
                connection, params=(*symbols, start, cutoff),
            )
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        return {**result, "status": "unavailable", "reason": f"缓存读取失败: {type(exc).__name__}"}
    volumes = pd.to_numeric(history["volume"], errors="coerce")
    history.loc[~volumes.between(0, float("inf"), inclusive="neither"), "close"] = float("nan")
    panel = history.pivot(index="date", columns="code", values="close")
    panel.index = pd.to_datetime(panel.index)
    latest = history["date"].max()
    if (pd.Timestamp(cutoff) - pd.Timestamp(latest)).days > 7:
        return {**result, "data_as_of": latest, "universe_size": len(symbols), "status": "stale",
                "reason": "最新日线距截止日超过7个自然日；不生成当前形态"}
    result.update({"data_as_of": latest, "universe_size": len(symbols), "status": "ok"})
    requested = set(codes or symbols)
    for code, frame in history.groupby("code", sort=True):
        if code not in requested:
            continue
        if frame["date"].max() != latest:
            result["items"].append({"code": code, "mode": "shadow", "status": "stale", "signals": []})
            continue
        result["items"].append({"code": code, **evaluate_patterns(code, frame, as_of=cutoff, universe_closes=panel)})
    result["missing_codes"] = sorted(requested - set(history["code"]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", help="已结束的信号日 YYYY-MM-DD")
    parser.add_argument("--stocks", nargs="+")
    parser.add_argument("--limit", type=int, default=800)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    print(json.dumps(screen_cached_universe(db_path=args.db, as_of=args.as_of, codes=args.stocks, limit=args.limit), ensure_ascii=False, default=str, allow_nan=False))


if __name__ == "__main__":
    main()
