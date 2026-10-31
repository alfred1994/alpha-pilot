"""冻结模型的前向观察：现场记录信号，严格成交数据不足时不报告账户收益。"""
from dataclasses import asdict, replace
from datetime import datetime, time, timedelta, timezone
import gzip
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

from portfolio.factor_backtest import ExecutionConfig, run_portfolio
from research.audit import audit_panel, encode, fingerprint
from research.ml_diagnostics import parse_calendar
from strategy.etf_research import BROAD_ETFS, validate_etf_panel
from strategy.factor_research import validate_panel
from strategy.ml_research import (FEATURE_COLS, FEATURE_VERSION, MODEL_PARAMS, MLResearchConfig,
                                  _make_model, _prediction_metrics, build_samples)

BJ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]


def implementation():
    files = ("research/ml_forward.py", "research/ml_diagnostics.py", "research/audit.py",
             "strategy/ml_research.py", "strategy/factor_research.py", "strategy/ml_baseline.py",
             "strategy/qlib_signal.py", "strategy/etf_research.py", "portfolio/factor_backtest.py",
             "scripts/run_ml_forward.py")
    digest = hashlib.sha256()
    for name in files:
        digest.update(name.encode())
        digest.update((ROOT/name).read_bytes())
    return {"source_sha256": digest.hexdigest(), "libraries": {name: version(name) for name in ("numpy", "pandas", "lightgbm")}}


def clock(now=None):
    now = now or datetime.now(BJ)
    if now.tzinfo is None:
        raise ValueError("前向观察必须使用带时区的实际时间")
    return now.astimezone(BJ)


def calendar_days(payload):
    if not payload or not payload.get("data"):
        raise ValueError("需要完整独立交易日历")
    dates = [r[0] for r in payload["data"]]
    return parse_calendar(payload, min(dates), max(dates))


def latest_closed(sessions, now):
    today = now.date().isoformat()
    if not sessions or today > sessions[-1]:
        raise ValueError("独立日历未覆盖当前日期，先补充日历证据")
    closed = [day for day in sessions if day < today or (day == today and now.time() >= time(15, 15))]
    if not closed:
        raise ValueError("没有已收盘交易日")
    return closed[-1]


def check_deadline(day, sessions, now):
    future = [d for d in sessions if d > day]
    if not future:
        raise ValueError("独立日历未覆盖下一交易日")
    deadline = datetime.fromisoformat(future[0]).replace(hour=9, minute=30, tzinfo=BJ)
    if now >= deadline:
        raise ValueError("信号记录已晚于下一交易日开盘，禁止事后补造信号")


def frame_records(frame):
    result = frame.copy()
    result["date"] = pd.to_datetime(result.date).dt.strftime("%Y-%m-%d")
    return json.loads(result.to_json(orient="records", double_precision=15))


def pack(frame):
    return gzip.compress(encode(frame_records(frame)).encode("utf-8"), mtime=0)


def unpack(value):
    frame = pd.DataFrame(json.loads(gzip.decompress(value)))
    frame["date"] = pd.to_datetime(frame.date)
    return frame


def _normalize(panel, benchmarks, sessions, expected, day):
    stocks = validate_panel(pd.DataFrame(panel))
    etfs = validate_etf_panel(pd.DataFrame(benchmarks))
    if set(stocks.code) - set(expected) or set(etfs.code) != set(BROAD_ETFS):
        raise ValueError("前向股票池/ETF范围与冻结协议不一致")
    for frame, codes in ((stocks, expected), (etfs, list(BROAD_ETFS))):
        days = frame.date.dt.strftime("%Y-%m-%d")
        if set(days) - set(sessions) or (days > day).any():
            raise ValueError("包含未收盘、未来或非交易日行情")
        if set(frame.loc[days == day, "code"]) != set(codes):
            raise ValueError("最新已收盘日覆盖不足；停牌须提供明确的零量/不可交易记录")
    return stocks, etfs


def _scores(model, samples, day, config):
    current = samples[samples.date == pd.Timestamp(day)].copy()
    if len(current) < config.min_stocks:
        raise ValueError("当前有效因子股票数量不足")
    current["score"] = model.predict(current[FEATURE_COLS], num_threads=config.threads)
    if not np.isfinite(current.score).all():
        raise ValueError("模型返回无效预测")
    return current[["date", "code", "score", "return_20"]]


def _weights(scores, config, execution, initial=False):
    result = {}
    for name, col in (("lightgbm", "score"), ("momentum20", "return_20")):
        selected = scores.sort_values([col, "code"], ascending=[False, True]).head(config.top_k)
        result[name] = {code: min(1/config.top_k, execution.max_weight) for code in selected.code}
    if initial:
        result.update({code: {code: 1.0} for code in BROAD_ETFS})
    return result


def _read_manifest(db):
    row = db.execute("SELECT payload FROM manifest WHERE id=1").fetchone()
    if row is None:
        raise ValueError("前向实验未完成初始化")
    return json.loads(row[0])


def _ingest_execution(db, payload, sessions, universe, initial, current):
    """执行行情可晚到，信号不能晚造；已使用的成交输入不可改写。"""
    if not payload:
        return
    metadata = payload.get("metadata") or {}
    if metadata.get("universe") != "forward_observed":
        raise ValueError("前向成交数据须声明现场冻结的股票池")
    rows = pd.DataFrame(payload.get("bars") or [])
    if rows.empty:
        return
    audit = audit_panel(rows, sessions, metadata)
    if audit["errors"]:
        raise ValueError("执行数据无效: " + ",".join(audit["errors"]))
    if audit["status"] != "strict_ready":
        # 不完整输入只记录缺口，不冻结成无法补齐的成交事实。
        return {"status": "waiting_execution_data", "reasons": audit["gaps"], "accounts": None}
    for row in frame_records(rows):
        if row["code"] not in set(universe) | set(BROAD_ETFS) or not initial <= row["date"] <= current:
            raise ValueError("执行输入超出冻结股票池或已观察日期")
        old = db.execute("SELECT payload FROM execution_rows WHERE day=? AND code=?", (row["date"], row["code"])).fetchone()
        serialized = encode(row)
        if old and old[0] != serialized:
            raise ValueError("已冻结的执行行情发生修订")
        db.execute("INSERT OR IGNORE INTO execution_rows VALUES(?,?,?)", (row["date"], row["code"], serialized))
    evidence = {key: value for key, value in metadata.items() if key not in ("start", "end")}
    previous = db.execute("SELECT payload FROM execution_metadata WHERE id=1").fetchone()
    if previous and json.loads(previous[0]) != evidence:
        raise ValueError("执行数据来源/公司行动声明变化，需重新核验")
    db.execute("INSERT OR IGNORE INTO execution_metadata VALUES(1,?)", (encode(evidence),))


def _accounts(db, manifest, sessions, current, previous_result):
    rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM execution_rows ORDER BY day,code")]
    meta = db.execute("SELECT payload FROM execution_metadata WHERE id=1").fetchone()
    if not rows or not meta:
        return {"status": "waiting_execution_data", "reasons": ["strict_execution_metadata_missing"], "accounts": None}
    days = [d for d in sessions if manifest["initial_date"] <= d <= current]
    metadata = dict(json.loads(meta[0]), start=days[0], end=days[-1])
    audit = audit_panel(rows, days, metadata, manifest["universe"] + list(BROAD_ETFS))
    if audit["status"] != "strict_ready":
        return {"status": "waiting_execution_data", "reasons": audit["errors"] + audit["gaps"], "accounts": None}
    if current == manifest["initial_date"]:
        return {"status": "awaiting_new_session", "reasons": [], "accounts": None}
    panel = pd.DataFrame(rows)
    panel["date"] = pd.to_datetime(panel.date)
    execution = ExecutionConfig(**manifest["execution"])
    books = {}
    signals = db.execute("SELECT day,observed_at,weights FROM days ORDER BY day").fetchall()
    for name in ("lightgbm", "momentum20", *BROAD_ETFS):
        targets = {}
        for day, observed_at, weights in signals:
            weights = json.loads(weights)
            if day >= current or name not in weights:
                continue
            check_deadline(day, sessions, datetime.fromisoformat(observed_at))
            targets[pd.Timestamp(day)] = weights[name]
        if not targets:
            raise ValueError("没有开盘前记录的目标")
        etf = name in BROAD_ETFS
        subset = panel[panel.code == name] if etf else panel[panel.code.isin(manifest["universe"])]
        cfg = replace(execution, max_positions=1, max_weight=1, stamp_tax=0, price_tick=.001) if etf else execution
        book = run_portfolio(subset, targets, cfg)
        prior = (previous_result.get("execution", {}).get("accounts") or {}).get(name)
        if prior and book["curve"][:len(prior["curve"])] != prior["curve"]:
            raise ValueError("历史前向净值发生变化")
        books[name] = book
    return {"status": "strict_simulation", "reasons": [], "accounts": books,
            "execution_verified": False, "note": "日线模拟，不保证集合竞价排队/市场冲击下可成交"}


def initialize(path, panel, benchmarks, calendar_payload, *, expected_codes=None,
               config=None, capital=100_000, evaluation_days=None, execution_payload=None, now=None):
    started_at = clock(now)
    sessions = calendar_days(calendar_payload)
    day = latest_closed(sessions, started_at)
    check_deadline(day, sessions, started_at)
    config = config or MLResearchConfig()
    config.validate()
    evaluation_days = config.valid_days if evaluation_days is None else evaluation_days
    if not isinstance(evaluation_days, int) or isinstance(evaluation_days, bool) or not 1 <= evaluation_days <= config.valid_days:
        raise ValueError("固定模型观察期须在 1 至 valid_days 内；到期重新登记，不能事后延长")
    execution = ExecutionConfig(capital=capital, max_positions=config.top_k, max_weight=min(.2, 1/config.top_k), mode="strict")
    execution.validate()
    universe = sorted(set(expected_codes or pd.DataFrame(panel).code))
    stocks, etfs = _normalize(panel, benchmarks, sessions, universe, day)
    # 训练和后续重放从同一精度的冻结输入开始，不能首次使用额外的小数精度。
    stock_blob, etf_blob = pack(stocks), pack(etfs)
    stocks, etfs = unpack(stock_blob), unpack(etf_blob)
    dates = [pd.Timestamp(d) for d in sessions if stocks.date.min() <= pd.Timestamp(d) <= pd.Timestamp(day)]
    samples = build_samples(stocks, dates, config.horizon, execution)
    train_dates = dates[-config.train_days-1:-1]
    if len(train_dates) != config.train_days:
        raise ValueError("训练历史不足")
    train = samples[samples.date.isin(train_dates) & (samples.label_end < pd.Timestamp(day)) & samples.target.notna()]
    if len(train) < max(100, config.min_stocks*20) or train.date.nunique() < config.train_days-config.horizon-2:
        raise ValueError("成熟训练样本或覆盖不足")
    before = implementation()
    model = _make_model(config, None)
    low, high = train.target.quantile([.01, .99])
    weights = 1/train.groupby("date")["code"].transform("count")
    model.fit(train[FEATURE_COLS], train.target.clip(low, high), sample_weight=weights/weights.mean())
    model_text = model.booster_.model_to_string()
    scores = _scores(model, samples, day, config)
    recorded_at = clock(now)
    check_deadline(day, sessions, recorded_at)
    if implementation() != before:
        raise ValueError("训练期间实现发生变化")
    manifest = {
        "version": "ml-forward-v1", "created_at": recorded_at.isoformat(), "initial_date": day,
        "interval_role": "forward", "promotion_allowed": False, "universe": universe,
        "config": asdict(config), "execution": asdict(execution), "evaluation_days": evaluation_days,
        "feature_version": FEATURE_VERSION, "model_params": dict(MODEL_PARAMS, n_jobs=config.threads),
        "implementation": before, "model_sha256": hashlib.sha256(model_text.encode()).hexdigest(),
        "seed_sha256": hashlib.sha256(stock_blob + etf_blob).hexdigest(),
        "calendar": sessions, "calendar_source": "baostock_raw_trade_dates", "calendar_sha256": fingerprint(calendar_payload),
        "train_start": train.date.min().strftime("%Y-%m-%d"), "train_end": train.date.max().strftime("%Y-%m-%d"),
        "train_label_end": train.label_end.max().strftime("%Y-%m-%d"), "train_rows": len(train),
        "train_target_clip": [float(low), float(high)], "model_policy": "fixed_until_observation_end_no_retraining",
        "limitations": ["固定当前股票池，从登记之后观察；不证明历史股票池无偏差。",
                        "因子使用冻结复权历史，提供的重叠历史修订会阻断；未提供的旧数据修订无法自动发现。",
                        "模型已见历史结果，只有登记后现场记录的预测属于前向观察。",
                        "缺严格执行行情时只保存预测，不把未成交记作现金收益或历史回测收益。"],
    }
    initial_weights = _weights(scores, config, execution, initial=True)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass
    db = sqlite3.connect(path, timeout=15)
    try:
        with db:
            db.executescript("""
                CREATE TABLE manifest(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
                CREATE TABLE seed(id INTEGER PRIMARY KEY CHECK(id=1),stocks BLOB,benchmarks BLOB,model TEXT);
                CREATE TABLE days(day TEXT PRIMARY KEY,observed_at TEXT,stocks BLOB,benchmarks BLOB,scores TEXT,weights TEXT,result TEXT);
                CREATE TABLE execution_rows(day TEXT,code TEXT,payload TEXT,PRIMARY KEY(day,code));
                CREATE TABLE execution_metadata(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT);
                CREATE TABLE calendar_versions(id INTEGER PRIMARY KEY,payload TEXT);
            """)
            db.execute("INSERT INTO manifest VALUES(1,?)", (encode(manifest),))
            db.execute("INSERT INTO seed VALUES(1,?,?,?)", (stock_blob, etf_blob, model_text))
            db.execute("INSERT INTO calendar_versions(payload) VALUES(?)", (encode(sessions),))
            execution_wait = _ingest_execution(db, execution_payload, sessions, universe, day, day)
            result = {"status": "initialized", "data_date": day, "observed_days": 0, "predicted_stocks": len(scores),
                      "recorded_at": recorded_at.isoformat(), "initial_capital_per_account": capital,
                      "promotion_allowed": False, "mature_label_rows": 0,
                      "execution": execution_wait or _accounts(db, manifest, sessions, day, {})}
            db.execute("INSERT INTO days VALUES(?,?,?,?,?,?,?)", (day, recorded_at.isoformat(),
                       pack(stocks[stocks.date == pd.Timestamp(day)]), pack(etfs[etfs.date == pd.Timestamp(day)]),
                       encode(frame_records(scores)), encode(initial_weights), encode(result)))
        return result
    finally:
        db.close()


def _merge_history(history, incoming, current):
    past = incoming[incoming.date < pd.Timestamp(current)]
    old = {(r["date"],r["code"]): r for r in frame_records(history)}
    for row in frame_records(past):
        if old.get((row["date"], row["code"])) != row:
            raise ValueError("已冻结的特征行情发生修订，不改写旧输入")
    return pd.concat([history, incoming[incoming.date == pd.Timestamp(current)]], ignore_index=True).sort_values(["date", "code"])


def advance(path, panel, benchmarks, calendar_payload, *, execution_payload=None, now=None):
    instant = clock(now)
    db = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=rw", uri=True, timeout=15)
    try:
        db.execute("BEGIN IMMEDIATE")
        manifest = _read_manifest(db)
        if manifest["implementation"] != implementation():
            raise ValueError("冻结的代码/依赖指纹已改变")
        last = db.execute("SELECT day,result FROM days ORDER BY day DESC LIMIT 1").fetchone()
        previous = json.loads(last[1])
        if previous["observed_days"] >= manifest["evaluation_days"]:
            return dict(previous, status="completed")
        sessions = calendar_days(calendar_payload)
        original = json.loads(db.execute("SELECT payload FROM calendar_versions ORDER BY id DESC LIMIT 1").fetchone()[0])
        if [day for day in sessions if day <= original[-1]] != original:
            raise ValueError("独立日历修订了已冻结区间")
        current = latest_closed(sessions, instant)
        if current == last[0]:
            stocks, etfs = _normalize(panel, benchmarks, sessions, manifest["universe"], current)
            last_stocks, last_etfs = db.execute("SELECT stocks,benchmarks FROM days WHERE day=?", (current,)).fetchone()
            for incoming, saved in ((stocks, unpack(last_stocks)), (etfs, unpack(last_etfs))):
                current_rows = incoming[incoming.date == pd.Timestamp(current)].sort_values(["date", "code"])
                if frame_records(current_rows) != frame_records(saved):
                    raise ValueError("已冻结的当日行情发生修订")
            return dict(previous, status="unchanged")
        if current != next((day for day in sessions if day > last[0]), None):
            raise ValueError("遗漏或倒退观察日期；禁止回填历史信号")
        check_deadline(current, sessions, instant)
        stocks, etfs = _normalize(panel, benchmarks, sessions, manifest["universe"], current)
        stock_blob, etf_blob, model_text = db.execute("SELECT stocks,benchmarks,model FROM seed WHERE id=1").fetchone()
        if hashlib.sha256(stock_blob+etf_blob).hexdigest() != manifest["seed_sha256"] or hashlib.sha256(model_text.encode()).hexdigest() != manifest["model_sha256"]:
            raise ValueError("冻结模型/输入校验失败")
        history, benchmark_history = unpack(stock_blob), unpack(etf_blob)
        for stock_day, etf_day in db.execute("SELECT stocks,benchmarks FROM days WHERE day>? ORDER BY day", (manifest["initial_date"],)):
            history = pd.concat([history, unpack(stock_day)], ignore_index=True)
            benchmark_history = pd.concat([benchmark_history, unpack(etf_day)], ignore_index=True)
        history = _merge_history(history, stocks, current)
        _merge_history(benchmark_history, etfs, current)
        config = MLResearchConfig(**manifest["config"])
        execution = ExecutionConfig(**manifest["execution"])
        samples = build_samples(history, [pd.Timestamp(d) for d in sessions if history.date.min() <= pd.Timestamp(d) <= pd.Timestamp(current)], config.horizon, execution)
        from lightgbm import Booster
        model = Booster(model_str=model_text)
        scores = _scores(model, samples, current, config)
        count = previous["observed_days"]+1
        weights = _weights(scores, config, execution) if count % config.horizon == 0 and count < manifest["evaluation_days"] else {}
        observed_at = clock(now)
        check_deadline(current, sessions, observed_at)
        execution_wait = _ingest_execution(db, execution_payload, sessions, manifest["universe"], manifest["initial_date"], current)
        saved_scores = [pd.DataFrame(json.loads(r[0])) for r in db.execute("SELECT scores FROM days ORDER BY day")]
        predictions = pd.concat(saved_scores, ignore_index=True)
        predictions["date"] = pd.to_datetime(predictions.date)
        evaluated = predictions.merge(samples[["date", "code", "target", "net_forward_return"]], on=["date", "code"], how="left", validate="one_to_one")
        result = {"status": "completed" if count >= manifest["evaluation_days"] else "advanced",
                  "data_date": current, "observed_days": count, "recorded_at": observed_at.isoformat(),
                  "predicted_stocks": len(scores), "promotion_allowed": False,
                  "mature_label_rows": int(evaluated.target.notna().sum()),
                  "label_metrics": _prediction_metrics(evaluated, config.min_stocks),
                  "execution": execution_wait or _accounts(db, manifest, sessions, current, previous)}
        db.execute("INSERT INTO days VALUES(?,?,?,?,?,?,?)", (current, observed_at.isoformat(),
                   pack(stocks[stocks.date == pd.Timestamp(current)]), pack(etfs[etfs.date == pd.Timestamp(current)]),
                   encode(frame_records(scores)), encode(weights), encode(result)))
        if sessions != original:
            db.execute("INSERT INTO calendar_versions(payload) VALUES(?)", (encode(sessions),))
        db.commit()
        return result
    finally:
        db.close()


def read_status(path):
    db = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)
    try:
        manifest = _read_manifest(db)
        row = db.execute("SELECT result FROM days ORDER BY day DESC LIMIT 1").fetchone()
        return {"manifest": manifest, "latest": json.loads(row[0])}
    finally:
        db.close()
