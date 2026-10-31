"""前向 ML 观察命令；使用真实当前时间，不允许历史回填或覆盖旧实验。"""
import argparse
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.ml_forward import advance, initialize, read_status


def load(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "advance", "status"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--benchmark-input", type=Path)
    parser.add_argument("--calendar-input", type=Path)
    parser.add_argument("--execution-input", type=Path, help="严格成交历史面板，必须包含原价、状态、涨跌停和来源声明")
    args = parser.parse_args(argv)
    if args.action == "status":
        report = read_status(args.state)
        result = report["latest"]
    else:
        if not all((args.input, args.benchmark_input, args.calendar_input)):
            parser.error("init/advance 需要行情和独立交易日历输入")
        stock, etf = load(args.input), load(args.benchmark_input)
        stocks = stock.get("bars", []) if isinstance(stock, dict) else stock
        benchmarks = etf.get("bars", []) if isinstance(etf, dict) else etf
        benchmarks = [row for row in benchmarks if row.get("code") in ("510300", "510500")]
        parameters = dict(execution_payload=load(args.execution_input) if args.execution_input else None)
        if args.action == "init":
            parameters["expected_codes"] = stock.get("expected_codes") if isinstance(stock, dict) else None
        result = (initialize if args.action == "init" else advance)(args.state, stocks, benchmarks, load(args.calendar_input), **parameters)
    summary = {k: result[k] for k in ("status", "data_date", "observed_days", "predicted_stocks", "mature_label_rows", "promotion_allowed") if k in result}
    summary["execution_status"] = result["execution"]["status"]
    summary["execution_reasons"] = result["execution"].get("reasons", [])
    if result["execution"].get("accounts"):
        summary["metrics"] = {name: account["metrics"] for name, account in result["execution"]["accounts"].items()}
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
