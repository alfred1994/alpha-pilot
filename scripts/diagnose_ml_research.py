"""只读核验冻结研究输入，输出新目录中的事后诊断，不修改原实验。"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from research.ml_diagnostics import diagnose, parse_calendar
from scripts.run_ml_research import file_hash, read_input, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--suspension-evidence", nargs="*", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = json.loads((args.experiment/"manifest.json").read_text(encoding="utf-8"))
    stock_input = manifest["input_files"]["stocks"]
    if file_hash(stock_input["file"]) != stock_input["sha256"]:
        raise ValueError("原研究输入哈希不一致")
    report = json.loads((args.experiment/"report.json").read_text(encoding="utf-8"))
    predictions = pd.read_csv(args.experiment/"predictions.csv.gz", dtype={"code": str})
    panel, _, _, _ = read_input(stock_input["file"], manifest["start"], manifest["end"])
    raw_calendar = json.loads(args.calendar.read_text(encoding="utf-8"))
    calendar = parse_calendar(raw_calendar, manifest["start"], manifest["end"])
    accounts = {name: json.loads((args.experiment/f"account-{name}.json").read_text(encoding="utf-8"))
                for name in ("lightgbm", "momentum20")}
    evidence = [json.loads(path.read_text(encoding="utf-8")) for path in args.suspension_evidence]
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output/"manifest.json", {"interval_role": "posthoc_diagnostic", "promotion_allowed": False,
        "source_experiment": str(args.experiment.resolve()), "report_sha256": file_hash(args.experiment/"report.json"),
        "predictions_sha256": file_hash(args.experiment/"predictions.csv.gz"),
        "calendar_sha256": file_hash(args.calendar), "evidence_sha256": {p.name: file_hash(p) for p in args.suspension_evidence}})
    result = diagnose(panel, predictions, report, accounts, calendar, evidence)
    write_json(args.output/"diagnostics.json", result)
    print(json.dumps({"status": result["status"], "calendar": result["calendar_audit"],
                      "folds": [{k:f[k] for k in ("fold","mean_daily_rank_ic","pending_horizon_rows","missing_realized_label_rows")} for f in result["folds"]],
                      "stale": {k:{key:v[key] for key in ("stale_days","confirmed_suspended_events","unresolved_events")} for k,v in result["stale_mark_audit"].items()}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
