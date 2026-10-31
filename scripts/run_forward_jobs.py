"""运行隔离的冻结因子/ML观察；一份公开行情供两项实验，失败保留明确状态。"""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid


def run(config):
    if os.environ.get("BROKER_MODE", "paper") != "paper":
        raise ValueError("forward jobs require paper mode")
    from scripts.run_ml_forward import load
    from scripts.collect_factor_forward import collect
    from research.ml_forward import calendar_days, clock, latest_closed, implementation, read_status, advance
    from strategy.factor_forward import read_status as factor_status, collection_seed, advance as factor_advance
    from scheduler.persistence import atomic_json_write, json_file_lock
    health_file = str(config["health_file"])
    with json_file_lock(health_file, timeout=1):
        old = load(health_file) if Path(health_file).exists() else {}
        report = {"attempted_at": clock().isoformat(), "status": "running", "jobs": {}, "promotion_allowed": False}
        atomic_json_write(health_file, report)
        try:
            calendar = load(config["calendar"])
            current = latest_closed(calendar_days(calendar), clock())
            pending = []
            for name, reader, writer in (("factor", factor_status, factor_advance), ("ml", read_status, advance)):
                try:
                    status = reader(config[name + "_state"])
                    matches = status.get("implementation_matches", status["manifest"]["implementation"] == implementation())
                    if not matches:
                        raise ValueError("implementation_changed")
                    previous = status["latest"]
                    if previous["observed_days"] >= status["manifest"]["evaluation_days"] or previous["data_date"] == current:
                        report["jobs"][name] = summary(previous, "unchanged")
                    elif current != next((d for d in calendar_days(calendar) if d > previous["data_date"]), None):
                        raise ValueError("missed_observation_day")
                    else:
                        pending.append((name, writer))
                except Exception as exc:
                    report["jobs"][name] = {"status": "blocked", "reason": str(exc)}
            if pending:
                folder = Path(config["snapshot_root"]) / (current + "-" + uuid.uuid4().hex[:10])
                result = collect(collection_seed(config["factor_state"]), calendar, folder, workers=2)
                if result["status"] != "ready":
                    raise ValueError("collection_incomplete")
                stock, etf = load(folder / "stocks.json.gz"), load(folder / "benchmarks.json.gz")
                for name, writer in pending:
                    try:
                        value = writer(config[name + "_state"], stock["bars"], etf["bars"], calendar)
                        report["jobs"][name] = summary(value, "ok")
                    except Exception as exc:
                        report["jobs"][name] = {"status": "blocked", "reason": str(exc)}
            report["status"] = "blocked" if any(v["status"] == "blocked" for v in report["jobs"].values()) else "ok"
        except Exception as exc:
            report.update(status="blocked", reason=str(exc))
        report["finished_at"] = clock().isoformat()
        report["last_success"] = report["finished_at"] if report["status"] == "ok" else old.get("last_success")
        atomic_json_write(health_file, report)
        return report


def summary(value, status):
    return {"status": status, "data_date": value["data_date"], "observed_days": value["observed_days"],
            "predicted_stocks": value.get("predicted_stocks"), "execution_status": value["execution"]["status"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    # 在冻结目录加载全部研究实现，主项目后续修复不会静默替换实验代码。
    sys.path.insert(0, str(Path(config["runtime_root"]).resolve()))
    result = run(config)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
