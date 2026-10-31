"""冻结全部因子对照模型，记录真实前向预测；支持只读状态和迟到成交证据结算。"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_ml_forward import load
from scripts.collect_factor_forward import collect
from research.ml_forward import calendar_days, clock, latest_closed
from strategy.factor_discovery import MODEL_PARAMS, MLResearchConfig, ResourceBudget, SearchConfig, feature_manifest, formula_catalog
from strategy.factor_forward import advance, collection_seed, initialize, read_status, reconcile


def daily(state, calendar_payload, snapshot_root, *, workers=2, execution_payload=None):
    status = read_status(state)
    if not status["implementation_matches"]:
        raise ValueError("冻结的代码/依赖指纹已改变")
    previous = status["latest"]
    if previous["observed_days"] >= status["manifest"]["evaluation_days"]:
        return dict(previous, status="completed")
    sessions = calendar_days(calendar_payload)
    current = latest_closed(sessions, clock())
    if current == previous["data_date"]:
        return dict(previous, status="unchanged")
    if current != next((day for day in sessions if day > previous["data_date"]), None):
        raise ValueError("遗漏观察日期，禁止每日任务补造历史预测")
    directory = Path(snapshot_root) / (current + "-" + uuid.uuid4().hex[:10])
    result = collect(collection_seed(state), calendar_payload, directory, workers=workers)
    if result["status"] != "ready":
        raise ValueError(f"当日行情未齐，缺口见 {directory / 'summary.json'}")
    stocks, benchmarks = load(directory / "stocks.json.gz"), load(directory / "benchmarks.json.gz")
    return advance(state, stocks["bars"], benchmarks["bars"], calendar_payload, execution_payload=execution_payload)


def compact(result):
    value = {k: result[k] for k in ("status", "data_date", "observed_days", "recorded_at", "settled_at", "predicted_stocks",
                                  "model_count", "mature_label_rows", "promotion_allowed", "assessment") if k in result}
    value["execution_status"] = result["execution"]["status"]
    value["execution_reasons"] = result["execution"].get("reasons", [])
    if result["execution"].get("accounts"):
        value["metrics"] = {name: book["metrics"] for name, book in result["execution"]["accounts"].items()}
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "advance", "status", "reconcile", "daily"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--benchmark-input", type=Path)
    parser.add_argument("--calendar-input", type=Path)
    parser.add_argument("--execution-input", type=Path)
    parser.add_argument("--protocol", type=Path, help="仅 init：研究命令产生的 manifest.json；冻结全部方案，不读收益或最优选择")
    parser.add_argument("--evaluation-days", type=int, default=60)
    parser.add_argument("--snapshot-root", type=Path, help="daily：保存每次采集结果的新子目录")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        if args.action == "status":
            status = read_status(args.state)
            value = compact(status["latest"])
            value["implementation_matches"] = status["implementation_matches"]
        elif args.action == "reconcile":
            if not args.execution_input:
                parser.error("reconcile 需要 --execution-input")
            value = compact(reconcile(args.state, load(args.execution_input)))
        elif args.action == "daily":
            if not args.calendar_input or not args.snapshot_root:
                parser.error("daily 需要 --calendar-input 和 --snapshot-root")
            value = compact(daily(args.state, load(args.calendar_input), args.snapshot_root, workers=args.workers,
                                  execution_payload=load(args.execution_input) if args.execution_input else None))
        else:
            if not all((args.input, args.benchmark_input, args.calendar_input)):
                parser.error("init/advance 需要行情与独立交易日历")
            if args.action != "init" and args.protocol:
                parser.error("推进期间不能更换协议")
            stock, etf = load(args.input), load(args.benchmark_input)
            stocks = stock.get("bars", []) if isinstance(stock, dict) else stock
            benchmarks = etf.get("bars", []) if isinstance(etf, dict) else etf
            benchmarks = [r for r in benchmarks if r.get("code") in ("510300", "510500")]
            params = dict(execution_payload=load(args.execution_input) if args.execution_input else None)
            if args.action == "init":
                protocol = load(args.protocol) if args.protocol else {}
                if protocol and protocol.get("kind") != "factor_discovery":
                    raise ValueError("协议必须为因子发现研究的 manifest")
                config = MLResearchConfig(**protocol.get("config", asdict(MLResearchConfig())))
                search = SearchConfig(**protocol.get("search_config", asdict(SearchConfig())))
                resources = ResourceBudget(**protocol.get("resource_limits", asdict(ResourceBudget())))
                if protocol and (protocol.get("features") != feature_manifest()
                                 or protocol.get("formula_catalog") != formula_catalog(search)
                                 or protocol.get("model_params") != dict(MODEL_PARAMS, n_jobs=config.threads)):
                    raise ValueError("协议的特征、公式或模型参数与当前实现不一致")
                params.update(config=config, search=search, resources=resources,
                              capital=protocol.get("execution", {}).get("capital", 100_000),
                              evaluation_days=args.evaluation_days,
                              protocol=protocol,
                              expected_codes=stock.get("expected_codes") if isinstance(stock, dict) else None)
            result = (initialize if args.action == "init" else advance)(args.state, stocks, benchmarks, load(args.calendar_input), **params)
            value = compact(result)
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc), "promotion_allowed": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
