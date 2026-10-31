"""隔离ETF前向研究账本：先保存信号，后以新收盘行情模拟次日开盘成交。

SQLite事务保证重复/并发运行幂等；输入和逐日结果保留，不改正式账户。
不修订旧成交，不追补遗漏日期，不处理公司行动；遇到这些情况明确阻断。
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sqlite3

import pandas as pd
import numpy as np

from portfolio.factor_backtest import ExecutionConfig, run_portfolio
from strategy.etf_research import BROAD_ETFS, SECTOR_ETFS, build_etf_targets, validate_etf_panel

SHANGHAI = timezone(timedelta(hours=8))
MODES = ("sector_equal", "sector_trend", "sector_rotation")
ROOT = Path(__file__).resolve().parents[1]


def strategy_hash():
    digest = hashlib.sha256()
    for file in ("strategy/etf_research.py", "portfolio/factor_backtest.py", "scheduler/etf_forward.py", "scripts/run_etf_forward.py"):
        digest.update(file.encode())
        digest.update((ROOT / file).read_bytes())
    return digest.hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def frame_json(frame):
    saved=frame.copy()
    saved["date"]=pd.to_datetime(saved.date).dt.strftime("%Y-%m-%d")
    # pandas.to_json默认截断浮点精度会让下次重放的旧成交发生微小变化。
    return encode(saved.to_dict(orient="records"))


def check_previous_close(previous_panel, incoming_panel, saved_raw, date):
    """仅接受原始快照佐证的两位→三位精度恢复；不覆盖历史特征或成交。

    KT最新日线可能保留两位小数，次日历史行恢复ETF三位精度。
    不能仅放宽绝对误差：小价格ETF即使0.001的真实修订也会影响信号。
    """
    stamp = pd.Timestamp(date)
    old = previous_panel[previous_panel.date == stamp].set_index("code").close
    incoming = incoming_panel[incoming_panel.date == stamp].set_index("code").close
    if old.index.has_duplicates or incoming.index.has_duplicates or set(old.index) != set(incoming.index):
        raise ValueError("供应商修订了既有复权价格或覆盖，需要核验；不改历史输入")
    raw = saved_raw.copy()
    raw["date"] = pd.to_datetime(raw.date)
    raw = raw[raw.date == stamp].set_index("code").close
    if raw.index.has_duplicates:
        raise ValueError("已保存原始快照重复，不能用于精度核验")
    audit = []
    for code in sorted(old.index):
        before, after = float(old[code]), float(incoming[code])
        if np.isclose(before, after, rtol=1e-7, atol=1e-7):
            continue
        reference = float(raw[code]) if code in raw.index else float("nan")
        if not all(np.isfinite(v) and v > 0 for v in (before, after, reference)):
            raise ValueError(f"供应商修订了既有复权价格 {code}，缺少有效原始快照佐证")
        previous, current, original = map(lambda value: Decimal(str(value)), (before, after, reference))
        supported = (
            previous == previous.quantize(Decimal("0.01"))
            and current == current.quantize(Decimal("0.001"))
            and current == original
            and current.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == previous
        )
        if not supported:
            raise ValueError(f"供应商修订了既有复权价格 {code}，不符合原始快照佐证的精度恢复；不改历史输入")
        audit.append({"code": code, "date": stamp.strftime("%Y-%m-%d"),
                      "saved_feature_close": before, "incoming_feature_close": after,
                      "saved_raw_close": reference,
                      "rule": "kt_two_to_three_decimals_matches_saved_raw_v1",
                      "historical_input_preserved": True})
    return audit


def _clock(now):
    if now.tzinfo is None:
        raise ValueError("必须提供带时区的当前时间")
    return now.astimezone(SHANGHAI)


def closed_panel(panel, now):
    now = _clock(now)
    cutoff = now.date() if (now.hour, now.minute) >= (15, 15) else now.date() - timedelta(days=1)
    bars = validate_etf_panel(panel)
    bars = bars[bars.date.dt.date <= cutoff].copy()
    expected = set(BROAD_ETFS) | {c for m in SECTOR_ETFS.values() for c in m}
    if set(bars.code) != expected or bars.empty:
        raise ValueError("17只ETF覆盖不足")
    if not (bars.groupby("date").size() == len(expected)).all():
        raise ValueError("日期覆盖不一致，禁止默默删除ETF/日期")
    return bars


def validate_raw(raw, bars):
    raw = validate_etf_panel(raw)
    date = bars.date.max()
    if set(raw.code) != set(bars.code) or len(raw) != bars.code.nunique() or not (raw.date == date).all():
        raise ValueError("未复权快照必须覆盖最新已收盘日的全部ETF")
    return raw


def _targets(bars, date):
    result = {}
    for mode in MODES:
        targets, _ = build_etf_targets(bars, mode, start=date, include_latest=True)
        result[mode] = targets[pd.Timestamp(date)]
    result.update({code:{code:1.0} for code in BROAD_ETFS})
    return result


def _connect(path):
    connection = sqlite3.connect(path, timeout=15)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize(path, panel, raw, adjustment_records, now=None, capital=100_000):
    now = _clock(now or datetime.now(SHANGHAI))
    if not np.isfinite(capital) or capital <= 0:
        raise ValueError("资金必须有限且为正")
    bars = closed_panel(panel, now)
    raw = validate_raw(raw, bars)
    date = bars.date.max().strftime("%Y-%m-%d")
    if (now.date() - bars.date.max().date()).days > 7:
        raise ValueError("初始化历史行情过旧")
    weights = _targets(bars, date)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # exclusive create避免重复初始化覆盖实验。失败的文件保留用于诊断。
    with path.open("xb"):
        pass
    connection = _connect(path)
    try:
        with connection:
            connection.execute("CREATE TABLE manifest (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
            connection.execute("CREATE TABLE observations (date TEXT PRIMARY KEY, observed_at TEXT NOT NULL, panel TEXT NOT NULL, raw TEXT NOT NULL, result TEXT NOT NULL)")
            connection.execute("CREATE TABLE signals (date TEXT NOT NULL, mode TEXT NOT NULL, recorded_at TEXT NOT NULL, weights TEXT NOT NULL, PRIMARY KEY(date,mode))")
            manifest = {"version":"etf-forward-v1", "created_at":now.isoformat(), "initial_data_date":date,
                        "capital":capital, "strategy_hash":strategy_hash(), "adjustments":adjustment_records,
                        "modes":list(MODES), "benchmarks":BROAD_ETFS, "primary":"sector_equal",
                        "rebalance_days":20,"evaluation_days":60,"promotion_allowed":False,
                        "execution":asdict(ExecutionConfig(capital=capital,max_positions=17,max_weight=1,stamp_tax=0,mode="research",price_tick=.001)),
                        "limitations":["收盘后用已事先记录的目标和当日开盘价模拟，非实时成交",
                                       "原始价格、假定10%限制及滑点；没有集合竞价排队/冲击，不能当成可实现收益",
                                       "供应商分红/拆分事件变化阻断，不能忽略现金分红或份额调整",
                                       "规则变更、缺日期均阻断；不得改历史账本或回填迟到信号"]}
            connection.execute("INSERT INTO manifest VALUES(1,?)", (encode(manifest),))
            for mode, target in weights.items():
                connection.execute("INSERT INTO signals VALUES(?,?,?,?)", (date,mode,now.isoformat(),encode(target)))
            result = {"status":"initialized", "data_date":date, "observed_days":0,"trade_count":0,
                      "capital_per_account":capital,"signals":weights,"recorded_at":now.isoformat(),
                      "next_action":"等待信号记录时间之后的新交易日行情；尚无前向收益", "promotion_allowed":False}
            connection.execute("INSERT INTO observations VALUES(?,?,?,?,?)",(date,now.isoformat(),frame_json(bars),frame_json(raw),encode(result)))
        return result
    finally:
        connection.close()


def advance(path, panel, raw, adjustment_records, now=None):
    now = _clock(now or datetime.now(SHANGHAI))
    if not Path(path).is_file():
        raise ValueError("实验未初始化")
    bars = closed_panel(panel, now)
    raw = validate_raw(raw,bars)
    current = bars.date.max().strftime("%Y-%m-%d")
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        manifest = json.loads(connection.execute("SELECT payload FROM manifest WHERE id=1").fetchone()[0])
        if manifest["strategy_hash"] != strategy_hash():
            raise ValueError("冻结的代码指纹发生变化；不得静默更改前向规则")
        last = connection.execute("SELECT date,result,panel,raw FROM observations ORDER BY date DESC LIMIT 1").fetchone()
        if current < last[0]:
            raise ValueError("行情时间倒退")
        if current == last[0]:
            connection.rollback()
            return {"status":"unchanged","data_date":current,"message":"无新已收盘交易日，不重复成交"}
        if encode(manifest["adjustments"]) != encode(adjustment_records):
            raise ValueError("分红/份额调整记录变化，需要核验公司行动；不按旧份额继续计收益")
        fresh_dates = sorted(bars.loc[bars.date>pd.Timestamp(last[0]),"date"].unique())
        if len(fresh_dates) != 1:
            raise ValueError(f"漏跑 {len(fresh_dates)} 个新行情日期；禁止事后补造信号/成交")
        previous_panel=pd.DataFrame(json.loads(last[2]))
        previous_panel["date"]=pd.to_datetime(previous_panel.date)
        precision_audit = check_previous_close(previous_panel, bars, pd.DataFrame(json.loads(last[3])), last[0])
        bars=pd.concat([previous_panel,bars[bars.date==pd.Timestamp(current)]],ignore_index=True).sort_values(["date","code"])
        rows = connection.execute("SELECT date,raw FROM observations ORDER BY date").fetchall()
        count = len(rows)
        if count > manifest["evaluation_days"]:
            connection.rollback()
            return {"status":"completed","observed_days":count-1,"promotion_allowed":False}
        raw_frames = [pd.DataFrame(json.loads(row[1])) for row in rows]
        ledger_bars = pd.concat(raw_frames+[raw],ignore_index=True)
        ledger_bars["date"]=pd.to_datetime(ledger_bars.date)
        results = {}
        config = ExecutionConfig(**manifest["execution"])
        for mode in list(MODES)+list(BROAD_ETFS):
            records = connection.execute("SELECT date,recorded_at,weights FROM signals WHERE mode=? ORDER BY date",(mode,)).fetchall()
            targets = {}
            for signal_date, recorded_at, weights in records:
                later = sorted(ledger_bars.loc[ledger_bars.date>pd.Timestamp(signal_date),"date"].unique())
                if not later:
                    continue
                open_at = pd.Timestamp(later[0]).to_pydatetime().replace(hour=9,minute=30,tzinfo=SHANGHAI)
                if datetime.fromisoformat(recorded_at) >= open_at:
                    raise ValueError("信号在执行日开盘后才记录，禁止回填当天开盘成交")
                targets[pd.Timestamp(signal_date)] = json.loads(weights)
            if not targets:
                raise ValueError("无开盘前记录的有效目标")
            book = run_portfolio(ledger_bars, targets, config)
            # 重放冻结输入用于复用已测试账本；历史净值不得因新一天输入而变化。
            previous = json.loads(last[1]).get("accounts",{}).get(mode)
            if previous and book["curve"][:-1] != previous["curve"]:
                raise ValueError("历史账户净值发生变化，停止实验")
            results[mode] = book
        weights = {}
        if count % manifest["rebalance_days"] == 0 and count < manifest["evaluation_days"]:
            # 信号记录的是实际运行时间。即使任务延迟也不能伪装成收盘时已知。
            weights = {k:v for k,v in _targets(bars,current).items() if k in MODES}
            for mode,target in weights.items():
                connection.execute("INSERT INTO signals VALUES(?,?,?,?)",(current,mode,now.isoformat(),encode(target)))
        result = {"status":"completed" if count>=manifest["evaluation_days"] else "advanced",
                  "data_date":current,"observed_days":count,"accounts":results,"new_signals":weights,
                  "promotion_allowed":False,"recorded_at":now.isoformat(),
                  "data_precision_audit":precision_audit}
        connection.execute("INSERT INTO observations VALUES(?,?,?,?,?)",(current,now.isoformat(),frame_json(bars),frame_json(raw),encode(result)))
        connection.commit()
        return result
    finally:
        connection.close()


def read_status(path):
    connection=sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)
    try:
        manifest=json.loads(connection.execute("SELECT payload FROM manifest WHERE id=1").fetchone()[0])
        row=connection.execute("SELECT result FROM observations ORDER BY date DESC LIMIT 1").fetchone()
        result=json.loads(row[0])
        return {"manifest":manifest,"latest":result}
    finally:
        connection.close()
