"""冻结扫描驱动的研究组合，重放不可变输入；不调用 LLM 或行情服务。"""
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, time
import json
import math

import pandas as pd

from portfolio.factor_backtest import ExecutionConfig, run_portfolio
from research.audit import audit_panel, encode, fingerprint, records
from research.store import BJ, code_version, connect, default_path, read_experiments, register, save_report

LIMITATIONS = [
    "仅比较最后一道 LLM 买入过滤；评分可能含上游 AI 信号。",
    "统一日频次日开盘研究账本，不是正式盘中账户收益，不模拟竞价排队或市场冲击。",
    "历史补录只能用于复查；只有实验创建后现场记录的扫描属于前向观察。",
    "60 个交易日只是首次评审点，不是统计显著性或策略自动晋级门槛。",
]


def clock(now=None):
    now = now or datetime.now(BJ)
    if now.tzinfo is None:
        raise ValueError("research clock requires timezone")
    return now.astimezone(BJ)


def llm_status(row):
    explicit = row.get("llm_status")
    if explicit in ("valid", "not_evaluated", "timeout", "parse_error", "unavailable", "error"):
        return explicit
    action = row.get("llm_action")
    if not action:
        return "not_evaluated"
    reason = str(row.get("llm_reason", ""))
    if "解析" in reason:
        return "parse_error"
    if "超时" in reason:
        return "timeout"
    if "无响应" in reason or "不可用" in reason:
        return "unavailable"
    confidence = row.get("llm_confidence")
    if action in ("BUY", "SELL", "HOLD") and isinstance(confidence, (int, float)) and confidence > 0:
        return "valid"
    return "error"


def normalize_scan(scan):
    stamp = clock(datetime.fromisoformat(scan["observed_at"]))
    day = stamp.date().isoformat()
    if scan["day"] != day or not scan.get("scan_id"):
        raise ValueError("invalid scan identity/date")
    params = scan["params"]
    top = params["top_k"]
    score, weight = float(params["min_score"]), float(params["max_weight"])
    if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= 5:
        raise ValueError("invalid top_k")
    if not math.isfinite(score) or not 0 <= score <= 100 or not math.isfinite(weight) or not 0 < weight <= .25:
        raise ValueError("invalid scan policy")
    rows, seen = [], set()
    for row in scan["candidates"]:
        code = row["code"]
        # 本轮限定普通主板股票，ETF 仅作为独立基准。
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit() or not code.startswith(("000", "001", "002", "003", "600", "601", "603", "605")):
            continue
        if code in seen:
            raise ValueError("duplicate scan candidate")
        seen.add(code)
        composite = float(row["composite"])
        if not math.isfinite(composite) or not 0 <= composite <= 100:
            raise ValueError("invalid candidate score")
        duration = row.get("llm_elapsed_seconds")
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
            duration = None
        rows.append({"code": code, "composite": composite, "llm_action": row.get("llm_action"),
                     "llm_status": llm_status(row), "llm_confidence": row.get("llm_confidence"),
                     "llm_elapsed_seconds": duration})
    return {"scan_id": scan["scan_id"], "day": day, "observed_at": stamp.isoformat(),
            "complete": scan.get("complete") is True,
            "strategy_version": scan.get("strategy_version", "unknown"),
            "params": {"top_k": top, "min_score": score, "max_weight": weight},
            "candidates": rows, "elapsed": scan.get("elapsed"),
            "llm_cost": None, "llm_cost_status": "not_recorded"}


def capture_scan(plan, path=None, now=None):
    now = clock(now)
    context = getattr(plan, "research_context", None)
    if not context or getattr(plan, "date", "") != now.date().isoformat():
        return False
    scan = normalize_scan({"scan_id": plan.scan_id, "day": plan.date, "observed_at": now.isoformat(),
                           "params": context, "candidates": plan.raw_scores,
                           "complete": getattr(plan, "research_complete", False),
                           "strategy_version": plan.strategy_version, "elapsed": plan.elapsed})
    with connect(path) as db:
        previous = db.execute("SELECT payload FROM scans WHERE scan_id=?", (scan["scan_id"],)).fetchone()
        if previous:
            return False  # 重试不能用稍晚时刻改写原扫描
        db.execute("INSERT INTO scans VALUES(?,?,?,?)", (scan["scan_id"], scan["day"], scan["observed_at"], encode(scan)))
    return True


def new_experiment(path=None, now=None, input_path=None, retrospective=False, start=None,
                   hypothesis="last-llm-filter-v1", parent_id=None, input_sha256=None):
    now = clock(now)
    start = start or now.date().isoformat()
    if not retrospective and start < now.date().isoformat():
        raise ValueError("forward experiment cannot start in the past")
    if pd.Timestamp(start).strftime("%Y-%m-%d") != start:
        raise ValueError("invalid start date")
    manifest = {"kind": "comparison", "hypothesis": hypothesis, "parent_id": parent_id,
                "protocol": "last-llm-filter-v1", "start": start,
                "registered_at": "unknown" if retrospective else now.isoformat(), "input_path": input_path,
                "input_sha256": input_sha256 or "forward_append_only",
                "interval_role": "review" if retrospective else "forward",
                "evaluation_days": 60, "cutoff": "14:50", "execution": asdict(ExecutionConfig()),
                "benchmarks": ["510300", "510500"], "promotion_allowed": False,
                "libraries": {"pandas": pd.__version__}}
    return register(manifest, path)


def _targets(scan):
    params = scan["params"]
    eligible = sorted((r for r in scan["candidates"] if r["composite"] >= params["min_score"]),
                      key=lambda r: (-r["composite"], r["code"]))[:params["top_k"]]
    weight = min(params["max_weight"], 1 / params["top_k"])
    a = {r["code"]: weight for r in eligible}
    b = {r["code"]: weight for r in eligible if r["llm_status"] == "valid" and r["llm_action"] == "BUY"}
    return a, b, eligible


def freeze_days(identity, manifest, calendar, as_of, path=None, imported_scans=None):
    if imported_scans and manifest["interval_role"] != "review":
        raise ValueError("forward experiments reject imported scans")
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        stored = [json.loads(r[0]) for r in db.execute("SELECT payload FROM scans ORDER BY observed_at,scan_id")]
        scans = [normalize_scan(s) for s in imported_scans] if imported_scans is not None else stored
        prior = {r[0]: json.loads(r[1]) for r in db.execute("SELECT day,payload FROM frozen_days WHERE experiment_id=?", (identity,))}
        prior_days = set(prior)
        sessions = sorted({d for d in calendar if manifest["start"] <= d <= as_of})
        if prior_days - set(sessions):
            raise ValueError("calendar revision would remove frozen sessions")
        for day in sessions:
            if day in prior_days and imported_scans is None:
                continue
            available = [s for s in scans if s["day"] == day and s["complete"]
                         and time(9, 30) <= datetime.fromisoformat(s["observed_at"]).time() <= time(14, 50)
                         and (manifest["interval_role"] == "review" or s["observed_at"] >= manifest["registered_at"])]
            scan = max(available, key=lambda s: (s["observed_at"], s["scan_id"])) if available else None
            payload = {"day": day, "scan": scan, "status": "captured" if scan else "missing_scan"}
            if day in prior_days:
                if payload != prior[day]:
                    raise ValueError("historical imported scan changed; create a new experiment")
                continue
            db.execute("INSERT INTO frozen_days VALUES(?,?,?)", (identity, day, encode(payload)))
        return [json.loads(r[0]) for r in db.execute("SELECT payload FROM frozen_days WHERE experiment_id=? ORDER BY day", (identity,))]


def _paired_samples(days, panel, calendar, config):
    indexed = {(r["date"], r["code"]): r for r in records(panel)}
    samples = []
    for item in days:
        if not item["scan"]:
            continue
        _, _, picks = _targets(item["scan"])
        future = [d for d in calendar if d > item["day"]]
        for row in picks:
            if row["llm_status"] != "valid":
                continue
            sample = {"day": item["day"], "code": row["code"], "action": row["llm_action"], "status": "pending"}
            window = [indexed.get((d, row["code"])) for d in future[:5]]
            if len(window) == 5 and all(window):
                entry, end = window[0], window[-1]
                if any(not r.get("tradable") or r["volume"] <= 0 for r in window) or entry["raw_open"] >= entry["limit_up"]:
                    sample["status"] = "untradable"
                else:
                    # 配对样本是标签，不假装具有组合整手/最低佣金账务。
                    net = end["raw_close"] / entry["raw_open"] - 1 - 2 * (config.commission + config.slippage) - config.stamp_tax
                    sample.update(status="mature", net_label_5d=net,
                                  filter_delta=0.0 if row["llm_action"] == "BUY" else -net)
            samples.append(sample)
    mature = [s for s in samples if s["status"] == "mature"]
    return {"samples": samples, "mature_count": len(mature),
            "mean_filter_delta": sum(s["filter_delta"] for s in mature) / len(mature) if mature else None,
            "basis": "有效判断的相同候选，次日开盘到第五日收盘减比例成本；非账户收益，不含整手最低佣金，标签重叠"}


def compare(identity, payload, as_of=None, path=None, now=None):
    from pathlib import Path
    import re
    from scheduler.persistence import json_file_lock
    if not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise ValueError("invalid experiment id")
    lock = Path(path or default_path()).resolve().with_name("research-" + identity)
    with json_file_lock(str(lock), timeout=1):
        return _compare(identity, payload, as_of, path, now)


def _compare(identity, payload, as_of=None, path=None, now=None):
    now = clock(now)
    as_of = as_of or now.date().isoformat()
    if as_of > now.date().isoformat() or (as_of == now.date().isoformat() and now.time() < time(15, 15)):
        raise ValueError("comparison requires a closed session")
    experiments = read_experiments(path, identity)
    if not experiments or experiments[0]["kind"] != "comparison":
        raise ValueError("unknown comparison experiment")
    experiment = experiments[0]
    manifest = experiment["manifest"]
    if experiment["status"] == "archived":
        raise ValueError("experiment archived")
    if manifest["code_version"] != code_version() or manifest["libraries"]["pandas"] != pd.__version__:
        raise ValueError("frozen implementation changed; create a new experiment")
    if experiment["latest"] and experiment["latest"]["as_of"] > as_of:
        raise ValueError("comparison date moved backwards")
    calendar = sorted(set(payload.get("calendar", [])))
    metadata = dict(payload.get("metadata") or {})
    scoped_calendar = [d for d in calendar if manifest["start"] <= d <= as_of]
    base = {"as_of": as_of, "status": "waiting_data", "verdict": "inconclusive", "metrics": {},
            "limitations": LIMITATIONS, "interval_role": manifest["interval_role"], "promotion_allowed": False}
    if not scoped_calendar or not metadata.get("calendar_source"):
        return save_report(identity, dict(base, reasons=["independent_calendar_required"]), path, "waiting_data")
    if any(pd.Timestamp(d).strftime("%Y-%m-%d") != d for d in calendar):
        raise ValueError("invalid calendar dates")
    days = freeze_days(identity, manifest, scoped_calendar, as_of, path, payload.get("scans"))
    base["selections"] = days
    base["observed_days"] = sum(bool(d["scan"]) for d in days)
    base["missing_scan_dates"] = [d["day"] for d in days if not d["scan"]]
    targets_a, targets_b, required = {}, {}, set(manifest["benchmarks"])
    status_counts = Counter()
    for item in days:
        if item["scan"]:
            a, b, picks = _targets(item["scan"])
            targets_a[pd.Timestamp(item["day"])] = a
            targets_b[pd.Timestamp(item["day"])] = b
            required.update(a)
            status_counts.update(r["llm_status"] for r in picks)
    base["llm_status_counts"] = dict(status_counts)
    durations = [r["llm_elapsed_seconds"] for d in days if d["scan"] for r in d["scan"]["candidates"]
                 if r.get("llm_elapsed_seconds") is not None]
    base["llm_latency"] = {"measured_calls": len(durations),
                           "mean_seconds": sum(durations) / len(durations) if durations else None,
                           "max_seconds": max(durations) if durations else None,
                           "basis": "已返回的个股决策调用耗时，包含上下文构建；超时未返回不纳入均值"}
    panel = pd.DataFrame(payload.get("bars", []))
    if not {"date", "code"}.issubset(panel):
        return save_report(identity, dict(base, reasons=["bars_required"]), path, "waiting_data")
    panel["date"] = pd.to_datetime(panel.date, errors="raise").dt.strftime("%Y-%m-%d")
    panel = panel[(panel.date >= manifest["start"]) & (panel.date <= as_of) & panel.code.isin(required)].copy()
    metadata.update(start=scoped_calendar[0], end=scoped_calendar[-1])
    if manifest["interval_role"] == "forward":
        metadata["universe"] = "forward_observed"
    audit = audit_panel(panel, scoped_calendar, metadata, sorted(required))
    base.update(audit=audit, input_sha256=audit["input_sha256"])
    if audit["status"] != "strict_ready":
        return save_report(identity, dict(base, reasons=audit["errors"] + audit["gaps"]), path, "waiting_data")
    # 同一实验已用于计算的行情永不覆盖，修订数据必须另开实验。
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        old = {(r[0], r[1]): r[2] for r in db.execute("SELECT day,code,payload FROM inputs WHERE experiment_id=?", (identity,))}
        fresh = {(r["date"], r["code"]): encode(r) for r in records(panel)}
        if any(key not in fresh or fresh[key] != value for key, value in old.items()):
            raise ValueError("historical input changed; create a new experiment")
        for (day, code), value in fresh.items():
            db.execute("INSERT OR IGNORE INTO inputs VALUES(?,?,?,?)", (identity, day, code, value))
    config = ExecutionConfig(**manifest["execution"])
    accounts = {}
    # 从相同起点保留现金；缺扫描时不产生新目标。
    origin = pd.Timestamp(scoped_calendar[0])
    for name, targets in (("A", targets_a), ("B", targets_b)):
        targets.setdefault(origin, {})
        stock_panel = panel[~panel.code.isin(manifest["benchmarks"])].copy()
        if stock_panel.empty:
            # 借用 ETF 日期轴，不下任何 ETF 订单，明确区分无扫描和有效空仓。
            stock_panel = panel[panel.code == "510300"].copy()
        accounts[name] = run_portfolio(stock_panel, targets, config)
    for name, code in zip(("C", "D"), manifest["benchmarks"]):
        accounts[name] = run_portfolio(panel[panel.code == code], {origin: {code: 1.0}},
                                       replace(config, max_weight=1.0, max_positions=1, stamp_tax=0, price_tick=.001))
    incomplete = any(any(r["stale_marks"] for r in b["curve"]) or any(k.startswith("missing_") for k in b["blocked_counts"]) for b in accounts.values())
    metrics = {k: dict(b["metrics"], mean_exposure=sum(1 - r["cash"] / r["equity"] for r in b["curve"]) / len(b["curve"]) if b["curve"] else None) for k, b in accounts.items()}
    base.update(accounts=accounts, metrics=metrics,
                net_return_delta=metrics["B"]["total_return"] - metrics["A"]["total_return"],
                paired=_paired_samples(days, panel, scoped_calendar, config),
                llm_cost=None, llm_cost_status="not_recorded",
                comparison_scope="last_buy_filter_only", protocol_sha256=fingerprint(manifest))
    old_report = experiment["latest"] or {}
    for name, account in (old_report.get("accounts") or {}).items():
        previous = account["curve"]
        if accounts[name]["curve"][:len(previous)] != previous:
            raise ValueError("historical equity changed; stop comparison")
    enough = base["observed_days"] >= manifest["evaluation_days"]
    reasons = []
    if not enough:
        reasons.append("insufficient_forward_sessions")
    if base["missing_scan_dates"]:
        reasons.append("missing_scans")
    if base["paired"]["mature_count"] < 30:
        reasons.append("insufficient_paired_samples")
    if manifest["interval_role"] != "forward":
        reasons.append("historical_review_not_new_holdout")
    deltas = [b["equity"] - a["equity"] for a, b in zip(accounts["A"]["curve"], accounts["B"]["curve"])]
    changes = [v - (deltas[i - 1] if i else 0) for i, v in enumerate(deltas)]
    total = sum(abs(v) for v in changes)
    concentration = sum(sorted((abs(v) for v in changes), reverse=True)[:5]) / total if total else None
    base["top5_delta_concentration"] = concentration
    if concentration is None or concentration > .5:
        reasons.append("effect_absent_or_concentrated")
    if incomplete:
        reasons.append("incomplete_execution")
    base.update(status="waiting_data" if incomplete else "completed" if enough else "running",
                review_due=enough, reasons=reasons,
                assessment="descriptive_comparison_only", verdict="inconclusive")
    # 无多重检验/独立显著性证据，本轮不自动将描述性领先宣判为支持。
    return save_report(identity, base, path, base["status"])
