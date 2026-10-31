"""固定因子实验的前向观察；六个模型共同冻结，只有事先记录的信号可结算。"""
from dataclasses import asdict, replace
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

from portfolio.factor_backtest import ExecutionConfig, run_portfolio
from research.audit import audit_panel, encode, fingerprint
from research.ml_forward import (
    _ingest_execution, _merge_history, _normalize, _read_manifest, calendar_days,
    check_deadline, clock, frame_records, implementation as baseline_implementation,
    latest_closed, pack, unpack,
)
from strategy.etf_research import BROAD_ETFS
from strategy.factor_discovery import (
    EXTENDED_COLS, FEATURE_VERSION, VARIANTS, MLResearchConfig, ResourceBudget,
    SearchConfig, discovery_samples, evaluate_formulas, feature_manifest,
    formula_catalog, select_formulas,
)
from strategy.ml_research import MODEL_PARAMS, _make_model, _prediction_metrics

ROOT = Path(__file__).resolve().parents[1]
MODEL_NAMES = (*VARIANTS, "formula_augmented")
ACCOUNT_NAMES = (*MODEL_NAMES, "momentum20", *BROAD_ETFS)


def implementation():
    base = baseline_implementation()
    digest = hashlib.sha256(base["source_sha256"].encode())
    for name in ("strategy/factor_discovery.py", "strategy/factor_forward.py", "scripts/run_factor_forward.py",
                 "scripts/collect_factor_forward.py", "data/kt_realtime.py", "data/history.py"):
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes())
    return dict(base, source_sha256=digest.hexdigest())


def _samples(stocks, sessions, day, config, execution, resources, catalog):
    if len(stocks) > resources.max_panel_rows:
        raise ValueError("超过已登记的行情行数预算")
    dates = [pd.Timestamp(d) for d in sessions if stocks.date.min() <= pd.Timestamp(d) <= pd.Timestamp(day)]
    samples = discovery_samples(stocks, dates, config.horizon, execution)
    if len(samples) * len(catalog) > resources.max_candidate_cells:
        raise ValueError("超过已登记的公式评估内存预算")
    return pd.concat([samples, evaluate_formulas(samples, catalog)], axis=1), dates


def _predict(models, features, samples, day, config):
    current = samples[samples.date == pd.Timestamp(day)].copy()
    if len(current) < config.min_stocks:
        raise ValueError("当前有效因子覆盖不足，不删除股票或降低门槛")
    scores = {}
    for name in MODEL_NAMES:
        result = current[["date", "code", "return_20"]].copy()
        result["score"] = models[name].predict(current[features[name]], num_threads=config.threads)
        if not np.isfinite(result.score).all():
            raise ValueError("模型产生非有限预测")
        scores[name] = frame_records(result)
    return scores


def _weights(scores, config, execution, initial=False):
    result = {}
    for name in (*MODEL_NAMES, "momentum20"):
        frame = pd.DataFrame(scores["baseline60" if name == "momentum20" else name])
        column = "return_20" if name == "momentum20" else "score"
        chosen = frame.sort_values([column, "code"], ascending=[False, True]).head(config.top_k)
        result[name] = {code: min(1 / config.top_k, execution.max_weight) for code in chosen.code}
    if initial:
        result.update({code: {code: 1.0} for code in BROAD_ETFS})
    return result


def _execution(db, manifest, sessions, current, previous):
    rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM execution_rows ORDER BY day,code")]
    meta = db.execute("SELECT payload FROM execution_metadata WHERE id=1").fetchone()
    if not rows or not meta:
        return {"status": "waiting_execution_data", "accounts": None, "reasons": ["strict_execution_metadata_missing"]}
    days = [d for d in sessions if manifest["initial_date"] <= d <= current]
    metadata = dict(json.loads(meta[0]), start=days[0], end=days[-1])
    audit = audit_panel(rows, days, metadata, manifest["universe"] + list(BROAD_ETFS))
    if audit["status"] != "strict_ready":
        return {"status": "waiting_execution_data", "accounts": None, "reasons": audit["errors"] + audit["gaps"]}
    if current == manifest["initial_date"]:
        return {"status": "awaiting_new_session", "accounts": None, "reasons": []}
    panel = pd.DataFrame(rows)
    panel["date"] = pd.to_datetime(panel.date)
    config = ExecutionConfig(**manifest["execution"])
    signals = db.execute("SELECT day,observed_at,weights FROM days ORDER BY day").fetchall()
    books = {}
    for name in ACCOUNT_NAMES:
        targets = {}
        for day, observed_at, weights in signals:
            weights = json.loads(weights)
            if day >= current or name not in weights:
                continue
            check_deadline(day, sessions, datetime.fromisoformat(observed_at))
            targets[pd.Timestamp(day)] = weights[name]
        if not targets:
            raise ValueError("缺少事先登记的目标")
        is_etf = name in BROAD_ETFS
        subset = panel[panel.code == name] if is_etf else panel[panel.code.isin(manifest["universe"])]
        execution = replace(config, max_positions=1, max_weight=1, stamp_tax=0, price_tick=.001) if is_etf else config
        book = run_portfolio(subset, targets, execution)
        old = (previous.get("execution", {}).get("accounts") or {}).get(name)
        if old and (book["curve"][:len(old["curve"])] != old["curve"] or book["trades"][:len(old["trades"])] != old["trades"]):
            raise ValueError("已结算的净值或成交发生变化")
        books[name] = book
    return {"status": "strict_simulation", "accounts": books, "reasons": [], "execution_verified": False,
            "note": "日线模拟，未验证集合竞价排队、市场冲击和真实成交"}


def _assessment(count, manifest, execution):
    if count < manifest["evaluation_days"]:
        return "awaiting_forward_observations"
    if execution["status"] != "strict_simulation":
        return "waiting_execution_data"
    return "review_required_no_automatic_promotion"


def _settle(db, manifest, sessions, current, previous, waiting):
    settlement = _execution(db, manifest, sessions, current, previous)
    if waiting and settlement["accounts"] is None:
        settlement["reasons"] = sorted(set(settlement["reasons"] + waiting.get("reasons", [])))
    return settlement


def _create_tables(db):
    db.executescript("""
        CREATE TABLE manifest(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
        CREATE TABLE seed(id INTEGER PRIMARY KEY CHECK(id=1),stocks BLOB,benchmarks BLOB);
        CREATE TABLE models(name TEXT PRIMARY KEY,model TEXT NOT NULL);
        CREATE TABLE days(day TEXT PRIMARY KEY,observed_at TEXT NOT NULL,stocks BLOB,benchmarks BLOB,scores TEXT,weights TEXT);
        CREATE TABLE assessments(id INTEGER PRIMARY KEY,day TEXT,checked_at TEXT,result TEXT NOT NULL);
        CREATE TABLE execution_rows(day TEXT,code TEXT,payload TEXT,PRIMARY KEY(day,code));
        CREATE TABLE execution_metadata(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT);
        CREATE TABLE calendar_versions(id INTEGER PRIMARY KEY,payload TEXT);
    """)


def initialize(path, panel, benchmarks, calendar_payload, *, expected_codes=None, config=None,
               search=None, resources=None, capital=100_000, evaluation_days=60,
               execution_payload=None, protocol=None, now=None):
    if Path(path).exists():
        raise ValueError("前向状态已存在，不能覆盖或重新训练")
    instant = clock(now)
    sessions = calendar_days(calendar_payload)
    day = latest_closed(sessions, instant)
    check_deadline(day, sessions, instant)
    before = implementation()
    config, search, resources = config or MLResearchConfig(), search or SearchConfig(), resources or ResourceBudget()
    config.validate()
    search.validate()
    resources.validate()
    if not isinstance(evaluation_days, int) or isinstance(evaluation_days, bool) or not 1 <= evaluation_days <= 250:
        raise ValueError("观察期须为 1 至 250 个新交易日")
    execution = ExecutionConfig(capital=capital, max_positions=config.top_k, max_weight=min(.2, 1 / config.top_k))
    execution.validate()
    universe = sorted(set(expected_codes or pd.DataFrame(panel).code))
    stocks, etfs = _normalize(panel, benchmarks, sessions, universe, day)
    stock_blob, etf_blob = pack(stocks), pack(etfs)
    stocks, etfs = unpack(stock_blob), unpack(etf_blob)
    catalog = formula_catalog(search)
    samples, dates = _samples(stocks, sessions, day, config, execution, resources, catalog)
    train_dates = dates[-config.train_days - 1:-1]
    train = samples[samples.date.isin(train_dates) & (samples.label_end < pd.Timestamp(day)) & samples.target.notna()]
    if (len(train_dates) != config.train_days or len(train) < max(100, config.min_stocks * 20)
            or train.date.nunique() < config.train_days - config.horizon - 2 or train.target.nunique() < 2):
        raise ValueError("成熟训练样本不足")
    selection = select_formulas(train, train_dates, catalog, search, config.min_stocks)
    features = {**VARIANTS, "formula_augmented": EXTENDED_COLS + selection["selected"]}
    low, high = train.target.quantile([.01, .99])
    weight = 1 / train.groupby("date").code.transform("count")
    models, texts, shared = {}, {}, {}
    for name, columns in features.items():
        key = tuple(columns)
        if key not in shared:
            model = _make_model(config, None)
            model.fit(train[columns], train.target.clip(low, high), sample_weight=weight / weight.mean())
            shared[key] = model.booster_
        models[name] = shared[key]
        texts[name] = models[name].model_to_string()
    scores = _predict(models, features, samples, day, config)
    observed = clock(now)
    check_deadline(day, sessions, observed)
    if implementation() != before:
        raise ValueError("训练期间代码发生变化")
    manifest = {
        "version": "factor-forward-v1", "initial_date": day, "created_at": observed.isoformat(),
        "interval_role": "forward", "promotion_allowed": False, "universe": universe,
        "config": asdict(config), "search_config": asdict(search), "resources": asdict(resources),
        "execution": asdict(execution), "evaluation_days": evaluation_days,
        "feature_version": FEATURE_VERSION, "feature_registry": feature_manifest(),
        "model_features": features, "model_params": dict(MODEL_PARAMS, n_jobs=config.threads),
        "parent_protocol_sha256": fingerprint(protocol) if protocol else None,
        "formula_catalog": catalog, "search": selection,
        "model_sha256": {name: hashlib.sha256(value.encode()).hexdigest() for name, value in texts.items()},
        "seed_sha256": hashlib.sha256(stock_blob + etf_blob).hexdigest(), "implementation": before,
        "calendar_source": "baostock_raw_trade_dates", "calendar_sha256": fingerprint(calendar_payload),
        "train_label_end": train.label_end.max().strftime("%Y-%m-%d"), "train_rows": len(train),
        "train_target_clip": [float(low), float(high)], "model_policy": "all_six_frozen_no_reselection_or_refit",
        "limitations": ["只有登记后现场记录的预测属于前向观察。", "全部模型并列观察，不按已看过的历史挑选赢家。",
                        "股票池为登记时冻结的存续池，不证明历史股票池无偏差。",
                        "复权或公司行动需持续核验，修订阻断；缺严格成交数据时收益不提供。",
                        "观察期结束仅生成待复核证据，不自动晋级、下单或延长观察。"],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass
    with closing(sqlite3.connect(path, timeout=30)) as db:
        _create_tables(db)
        with db:
            db.execute("INSERT INTO manifest VALUES(1,?)", (encode(manifest),))
            db.execute("INSERT INTO seed VALUES(1,?,?)", (stock_blob, etf_blob))
            db.executemany("INSERT INTO models VALUES(?,?)", texts.items())
            db.execute("INSERT INTO calendar_versions(payload) VALUES(?)", (encode(sessions),))
            db.execute("INSERT INTO days VALUES(?,?,?,?,?,?)", (day, observed.isoformat(),
                       pack(stocks[stocks.date == pd.Timestamp(day)]), pack(etfs[etfs.date == pd.Timestamp(day)]),
                       encode(scores), encode(_weights(scores, config, execution, initial=True))))
            waiting = _ingest_execution(db, execution_payload, sessions, universe, day, day)
            settlement = _settle(db, manifest, sessions, day, {}, waiting)
            result = {"status": "initialized", "data_date": day, "observed_days": 0,
                      "recorded_at": observed.isoformat(), "predicted_stocks": len(scores["baseline60"]),
                      "model_count": len(MODEL_NAMES), "initial_capital_per_account": capital,
                      "promotion_allowed": False, "execution": settlement, "mature_label_rows": 0,
                      "assessment": _assessment(0, manifest, settlement)}
            db.execute("INSERT INTO assessments(day,checked_at,result) VALUES(?,?,?)", (day, observed.isoformat(), encode(result)))
    return result


def _state(db):
    manifest = _read_manifest(db)
    if manifest.get("version") != "factor-forward-v1":
        raise ValueError("不是因子前向状态库")
    if manifest["implementation"] != implementation():
        raise ValueError("冻结的代码/依赖指纹已改变")
    stock_blob, etf_blob = db.execute("SELECT stocks,benchmarks FROM seed WHERE id=1").fetchone()
    texts = dict(db.execute("SELECT name,model FROM models"))
    if (hashlib.sha256(stock_blob + etf_blob).hexdigest() != manifest["seed_sha256"]
            or {name: hashlib.sha256(text.encode()).hexdigest() for name, text in texts.items()} != manifest["model_sha256"]):
        raise ValueError("冻结输入或模型校验失败")
    history, etfs = unpack(stock_blob), unpack(etf_blob)
    stock_days, etf_days = [history], [etfs]
    for s, e in db.execute("SELECT stocks,benchmarks FROM days WHERE day>? ORDER BY day", (manifest["initial_date"],)):
        stock_days.append(unpack(s))
        etf_days.append(unpack(e))
    latest = json.loads(db.execute("SELECT result FROM assessments ORDER BY id DESC LIMIT 1").fetchone()[0])
    sessions = json.loads(db.execute("SELECT payload FROM calendar_versions ORDER BY id DESC LIMIT 1").fetchone()[0])
    return manifest, pd.concat(stock_days, ignore_index=True), pd.concat(etf_days, ignore_index=True), texts, latest, sessions


def advance(path, panel, benchmarks, calendar_payload, *, execution_payload=None, now=None):
    instant = clock(now)
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=30)) as db:
        db.execute("BEGIN IMMEDIATE")
        manifest, history, etf_history, texts, previous, original = _state(db)
        if previous["observed_days"] >= manifest["evaluation_days"]:
            return dict(previous, status="completed")
        sessions = calendar_days(calendar_payload)
        if [d for d in sessions if d <= original[-1]] != original:
            raise ValueError("独立日历修订了已冻结区间")
        current = latest_closed(sessions, instant)
        stocks, etfs = _normalize(panel, benchmarks, sessions, manifest["universe"], current)
        last = previous["data_date"]
        if current == last:
            # 包括重叠历史在内全部核验，不能只比较当日而漏掉供应商历史修订。
            for incoming, saved in ((stocks, history), (etfs, etf_history)):
                _merge_history(saved, incoming, current)
                if frame_records(incoming[incoming.date == pd.Timestamp(current)].sort_values("code")) != frame_records(saved[saved.date == pd.Timestamp(current)].sort_values("code")):
                    raise ValueError("已冻结的当日行情发生修订")
            return dict(previous, status="unchanged")
        if current != next((d for d in sessions if d > last), None):
            raise ValueError("遗漏或倒退观察日期，禁止补造历史信号")
        check_deadline(current, sessions, instant)
        history = _merge_history(history, stocks, current)
        _merge_history(etf_history, etfs, current)
        config, execution = MLResearchConfig(**manifest["config"]), ExecutionConfig(**manifest["execution"])
        samples, _ = _samples(history, sessions, current, config, execution, ResourceBudget(**manifest["resources"]), manifest["formula_catalog"])
        from lightgbm import Booster
        models = {name: Booster(model_str=value) for name, value in texts.items()}
        scores = _predict(models, manifest["model_features"], samples, current, config)
        count = previous["observed_days"] + 1
        observed = clock(now)
        check_deadline(current, sessions, observed)
        weights = _weights(scores, config, execution) if count % config.horizon == 0 and count < manifest["evaluation_days"] else {}
        stored = [json.loads(row[0]) for row in db.execute("SELECT scores FROM days ORDER BY day")]
        label_metrics = {}
        for name in MODEL_NAMES:
            predictions = pd.DataFrame([r for batch in stored for r in batch[name]])
            predictions["date"] = pd.to_datetime(predictions.date)
            evaluated = predictions.merge(samples[["date", "code", "target", "net_forward_return"]], on=["date", "code"], how="left", validate="one_to_one")
            label_metrics[name] = _prediction_metrics(evaluated, config.min_stocks)
        waiting = _ingest_execution(db, execution_payload, sessions, manifest["universe"], manifest["initial_date"], current)
        settlement = _settle(db, manifest, sessions, current, previous, waiting)
        result = {"status": "completed" if count >= manifest["evaluation_days"] else "advanced", "data_date": current,
                  "observed_days": count, "recorded_at": observed.isoformat(), "predicted_stocks": len(scores["baseline60"]),
                  "model_count": len(MODEL_NAMES), "promotion_allowed": False, "label_metrics": label_metrics,
                  "mature_label_rows": label_metrics["baseline60"]["labelled_rows"], "execution": settlement,
                  "assessment": _assessment(count, manifest, settlement)}
        if implementation() != manifest["implementation"]:
            raise ValueError("观察期间实现发生变化")
        db.execute("INSERT INTO days VALUES(?,?,?,?,?,?)", (current, observed.isoformat(),
                   pack(stocks[stocks.date == pd.Timestamp(current)]), pack(etfs[etfs.date == pd.Timestamp(current)]), encode(scores), encode(weights)))
        db.execute("INSERT INTO assessments(day,checked_at,result) VALUES(?,?,?)", (current, observed.isoformat(), encode(result)))
        if sessions != original:
            db.execute("INSERT INTO calendar_versions(payload) VALUES(?)", (encode(sessions),))
        db.commit()
    return result


def reconcile(path, execution_payload, now=None):
    """迟到的成交证据只结算事先保存的信号；不产生预测、不改历史成交。"""
    instant = clock(now)
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=30)) as db:
        db.execute("BEGIN IMMEDIATE")
        manifest, _, _, _, previous, sessions = _state(db)
        current = previous["data_date"]
        waiting = _ingest_execution(db, execution_payload, sessions, manifest["universe"], manifest["initial_date"], current)
        settlement = _settle(db, manifest, sessions, current, previous, waiting)
        if settlement == previous["execution"]:
            return dict(previous, status="unchanged")
        result = dict(previous, execution=settlement, assessment=_assessment(previous["observed_days"], manifest, settlement))
        result["settled_at"] = instant.isoformat()
        db.execute("INSERT INTO assessments(day,checked_at,result) VALUES(?,?,?)", (current, instant.isoformat(), encode(result)))
        db.commit()
    return result


def read_status(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        manifest = _read_manifest(db)
        if manifest.get("version") != "factor-forward-v1":
            raise ValueError("不是因子前向状态库")
        latest = json.loads(db.execute("SELECT result FROM assessments ORDER BY id DESC LIMIT 1").fetchone()[0])
        return {"manifest": manifest, "latest": latest, "implementation_matches": manifest["implementation"] == implementation()}


def collection_seed(path):
    """只读重建冻结股票池和历史，供每日采集使用，不查询正式数据库。"""
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        manifest, history, _, _, _, _ = _state(db)
        return {"bars": frame_records(history), "expected_codes": manifest["universe"]}
