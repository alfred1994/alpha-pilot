"""离线因子增量评估、类别消融和受限公式搜索；只写新的独立研究目录。"""
import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio.factor_backtest import ExecutionConfig
from research.store import code_version, register, save_report
from scripts.run_ml_research import file_hash, peak_rss_mib, read_input, write_json
from strategy.factor_discovery import (
    FEATURE_VERSION, INNER_TRAIN_FRACTION, MAX_CANDIDATE_CELLS, MAX_PANEL_ROWS,
    MODEL_PARAMS, VARIANTS, MLResearchConfig, ResourceBudget, SearchConfig, feature_manifest,
    formula_catalog, run_discovery,
)


def source_version():
    """现有研究指纹不包含新增 strategy/scripts 文件，须显式纳入本实验。"""
    current = code_version()
    digest = hashlib.sha256(current["source_sha256"].encode())
    for name in ("strategy/factor_discovery.py", "scripts/run_factor_discovery.py"):
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes())
    return dict(current, source_sha256=digest.hexdigest())


def render_report(report):
    rows = ["# 因子增量与受限公式搜索", "", "历史研究，所有方案保留，不能自动晋级或据此宣布盈利有效。", ""]
    if report["status"] != "completed":
        return "\n".join(rows + ["未完成：" + ", ".join(report.get("reasons", [])), ""])
    rows += [f"股票数：{report['symbols']}；样本：{report['sample_rows']:,}；外层窗口：{len(report['folds'])}。",
             f"成交状态：{report['execution_status']}；数据审计：{report['data_audit']['status']}。", "",
             "|方案|累计收益|最大回撤|平均仓位|换手/日|成交笔数|旧价天数|",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name, values in report["metrics"].items():
        rows.append(f"|{name}|{values['total_return']:.2%}|{values['max_drawdown']:.2%}|{values['average_exposure']:.1%}|{values['mean_daily_turnover']:.2%}|{values['trade_count']}|{values['stale_mark_days']}|")
    rows += ["", "现金收益为 0；所有账户共用评估日期。ETF 免股票印花税。", "",
             "增量为前者减去对照；类别行是 extended66 减去删除该类别的模型。",
             "回撤使用负数，回撤差为正表示回撤减轻；增量和消融均不证明因果。", "",
             "|比较/移除类别|收益差|回撤差|日换手差|仓位差|", "|---|---:|---:|---:|---:|"]
    for name, values in report["increments"].items():
        rows.append(f"|{name}|{values['total_return']:+.2%}|{values['max_drawdown']:+.2%}|{values['mean_daily_turnover']:+.2%}|{values['average_exposure']:+.1%}|")
    rows += ["", "公式按各外层训练窗的内部发现/选择成绩筛选；不按本表挑选冠军。", ""]
    for fold in report["folds"]:
        selected = ", ".join(fold["search"]["selected"]) or "无公式通过；与 extended66 相同"
        rows.append(f"- 第 {fold['fold']} 窗：{selected}；训练标签截止 {fold['train_label_end']}；验证从 {fold['validation_start']} 开始。")
    for key in ("data_audit", "benchmark_audit"):
        rows += ["", key + " 缺口：" + ", ".join(report[key]["gaps"])]
    rows += [""] + ["- " + item for item in report["limitations"]]
    return "\n".join(rows) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--benchmark-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="必须是尚不存在的新目录")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--execution", choices=("strict", "research"), default="strict")
    parser.add_argument("--train-days", type=int, default=250)
    parser.add_argument("--valid-days", type=int, default=60)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--min-stocks", type=int, default=20)
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--formula-budget", type=int, default=48, help="预先冻结的公式数量，1 至 96")
    parser.add_argument("--keep-formulas", type=int, default=3, help="每窗最多保留 1 至 4 个公式")
    parser.add_argument("--max-panel-rows", type=int, default=MAX_PANEL_ROWS)
    parser.add_argument("--max-candidate-cells", type=int, default=MAX_CANDIDATE_CELLS)
    args = parser.parse_args(argv)
    config = MLResearchConfig(args.train_days, args.valid_days, args.horizon, args.top_k, args.min_stocks, args.threads)
    search = SearchConfig(max_candidates=args.formula_budget, max_selected=args.keep_formulas)
    config.validate()
    search.validate()
    resources = ResourceBudget(args.max_panel_rows, args.max_candidate_cells)
    resources.validate()
    start, end = datetime.fromisoformat(args.start_date), datetime.fromisoformat(args.end_date)
    if start.date().isoformat() != args.start_date or end.date().isoformat() != args.end_date or start > end:
        parser.error("日期必须为 YYYY-MM-DD，且开始不晚于结束")
    execution = ExecutionConfig(capital=args.capital, max_positions=config.top_k,
                                max_weight=min(.2, 1 / config.top_k), mode=args.execution)
    execution.validate()
    directory = args.output.resolve()
    if directory.exists():
        parser.error("输出目录已存在；不能覆盖旧实验")
    inputs = {name: {"file": str(path.resolve()), "sha256": file_hash(path)}
              for name, path in (("stocks", args.input), ("benchmarks", args.benchmark_input))}
    libraries = {}
    for name in ("numpy", "pandas", "lightgbm", "scikit-learn"):
        try:
            libraries[name] = version(name)
        except PackageNotFoundError:
            libraries[name] = None
    manifest = {
        "kind": "factor_discovery", "hypothesis": FEATURE_VERSION,
        "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "interval_role": "review", "promotion_allowed": False,
        "input_files": inputs, "start": args.start_date, "end": args.end_date,
        "config": asdict(config), "execution": asdict(execution), "search_config": asdict(search),
        "features": feature_manifest(), "variants": {**VARIANTS, "formula_augmented": "extended66 + inner-selected formulas"},
        "formula_catalog": formula_catalog(search), "max_formula_nodes": 3,
        "inner_train_fraction": INNER_TRAIN_FRACTION,
        "resource_limits": asdict(resources),
        "model_params": dict(MODEL_PARAMS, n_jobs=config.threads),
        "code_version": source_version(), "libraries": libraries,
        "benchmarks": ["momentum20_same_universe_top_k", "510300", "510500", "cash"],
        "selection_rule": "内部时间切分并按标签实现日隔离；外层验证仅报告，不调参或挑选方案；全体尝试保留",
    }
    directory.mkdir(parents=True)
    write_json(directory / "manifest.json", manifest)
    store_path = directory / "evidence.sqlite3"
    identity = register(manifest, store_path)
    started, cpu_start = time.monotonic(), time.process_time()
    try:
        panel, metadata, calendar, expected = read_input(args.input, args.start_date, args.end_date)
        benchmarks, benchmark_metadata, benchmark_calendar, _ = read_input(args.benchmark_input, args.start_date, args.end_date)
        if benchmark_calendar is not None and calendar != benchmark_calendar:
            raise ValueError("股票与基准声明的独立交易日历不一致")
        report = run_discovery(panel, benchmarks, config, execution, search, calendar=calendar,
                               metadata=metadata, benchmark_metadata=benchmark_metadata, expected_codes=expected,
                               resources=resources,
                               progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
        if any(file_hash(item["file"]) != item["sha256"] for item in inputs.values()):
            raise ValueError("实验期间输入文件发生变化，请使用冻结快照重新运行")
        if source_version() != manifest["code_version"]:
            raise ValueError("实验期间研究代码发生变化，请使用新目录重新运行")
        import pandas as pd
        for name, frame in report.pop("predictions", {}).items():
            pd.DataFrame(frame).to_csv(directory / f"predictions-{name}.csv.gz", index=False, compression="gzip")
        for name, book in report.pop("accounts", {}).items():
            write_json(directory / f"account-{name}.json", book)
        write_json(directory / "selections.json", report.pop("selections", {}))
        report["resources"] = {"wall_seconds": round(time.monotonic() - started, 3),
                               "cpu_seconds": round(time.process_time() - cpu_start, 3),
                               "peak_rss_mib": peak_rss_mib(), "python": sys.version.split()[0],
                               "platform": sys.platform, "threads": config.threads}
        report["as_of"] = args.end_date
        write_json(directory / "report.json", report)
        with (directory / "report.md").open("x", encoding="utf-8") as stream:
            stream.write(render_report(report))
        state = "completed" if report["status"] == "completed" else "waiting_data"
        save_report(identity, report, store_path, state)
        summary = {key: report[key] for key in ("status", "execution_status", "metrics", "increments", "resources", "reasons") if key in report}
        summary.update(experiment_id=identity, promotion_allowed=False, interval_role="review")
        write_json(directory / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, allow_nan=False), flush=True)
        return 0 if state == "completed" else 2
    except Exception as exc:
        failure = {"status": "failed", "as_of": args.end_date, "reason": str(exc),
                   "error_type": type(exc).__name__, "promotion_allowed": False}
        write_json(directory / "failure.json", failure)
        save_report(identity, failure, store_path, "waiting_data")
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
