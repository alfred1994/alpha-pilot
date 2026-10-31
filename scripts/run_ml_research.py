"""运行冻结配置的离线 ML 研究；只读取给定输入，产物写入新的独立目录。"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import gzip
import hashlib
from importlib.metadata import version, PackageNotFoundError
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio.factor_backtest import ExecutionConfig
from research.store import code_version, register, save_report
from strategy.ml_research import FEATURE_COLS, FEATURE_VERSION, MODEL_PARAMS, MLResearchConfig, run_ml_research


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_input(path, start, end):
    import pandas as pd
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    wrapper = payload if isinstance(payload, dict) else {}
    frame = pd.DataFrame(wrapper.get("bars", []) if wrapper else payload)
    if "date" not in frame:
        raise ValueError("输入缺少 bars/date")
    dates = pd.to_datetime(frame["date"], errors="raise")
    # 保留区间端点上的盘中记录交给审计拒绝，不能用午夜截止把坏记录悄悄过滤掉。
    days = dates.dt.normalize()
    frame = frame[(days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))].copy()
    metadata = dict(wrapper.get("metadata") or {}, start=start, end=end)
    calendar = wrapper.get("calendar")
    if calendar is not None:
        calendar = [day for day in calendar if start <= day <= end]
    return frame, metadata, calendar, wrapper.get("expected_codes")


def peak_rss_mib():
    """进程峰值驻留内存；不安装监控依赖，不包含操作系统文件缓存。"""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        query = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if query(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return round(counters.PeakWorkingSetSize / 1024 ** 2, 1)
        return None
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(value / (1024 ** 2 if sys.platform == "darwin" else 1024), 1)


def render_report(report):
    rows = ["# 机器学习研究基线", "", "历史回放，未自动晋级；以下为扣费后的研究估计。", ""]
    if report["status"] != "completed":
        return "\n".join(rows + ["未完成：" + ", ".join(report.get("reasons", [])), ""])
    rows += [f"股票数：{report['symbols']}；样本：{report['sample_rows']:,}；滚动窗口：{len(report['folds'])}；因子：{len(report['features'])}。",
             f"成交状态：{report['execution_status']}；数据审计：{report['data_audit']['status']}。", "",
             "|方案|累计收益|最大回撤|平均仓位|成交笔数|旧价估值天数|", "|---|---:|---:|---:|---:|---:|"]
    for name, metrics in report["metrics"].items():
        rows.append(f"|{name}|{metrics['total_return']:.2%}|{metrics['max_drawdown']:.2%}|{metrics['average_exposure']:.1%}|{metrics['trade_count']}|{metrics['stale_mark_days']}|")
    rows += ["", "现金基准收益为 0；ETF 免股票印花税，其余费用口径一致。",
             "", "数据缺口：" + ", ".join(report["data_audit"]["gaps"]), "",
             "完整逐窗指标、逐笔成交、受阻原因及预测见本目录 JSON/CSV；特征重要性不能证明因果。", ""]
    rows += ["- " + item for item in report["limitations"]]
    return "\n".join(rows) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="股票面板 JSON 或 JSON.gz；数组或含 bars/metadata/calendar 的对象")
    parser.add_argument("--benchmark-input", type=Path, required=True, help="含 510300、510500 的本地 ETF 面板")
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
    args = parser.parse_args(argv)
    config = MLResearchConfig(args.train_days, args.valid_days, args.horizon, args.top_k, args.min_stocks, args.threads)
    config.validate()
    start, end = datetime.fromisoformat(args.start_date), datetime.fromisoformat(args.end_date)
    if start.date().isoformat() != args.start_date or end.date().isoformat() != args.end_date or start > end:
        parser.error("日期必须为 YYYY-MM-DD，且开始不晚于结束")
    execution = ExecutionConfig(capital=args.capital, max_positions=config.top_k,
                                max_weight=min(0.2, 1 / config.top_k), mode=args.execution)
    execution.validate()
    directory = args.output.resolve()
    if directory.exists():
        parser.error("输出目录已存在；请使用新目录，不能覆盖旧实验")
    inputs = {"stocks": {"file": str(args.input.resolve()), "sha256": file_hash(args.input)},
              "benchmarks": {"file": str(args.benchmark_input.resolve()), "sha256": file_hash(args.benchmark_input)}}
    libraries = {}
    for name in ("numpy", "pandas", "lightgbm", "scikit-learn"):
        try:
            libraries[name] = version(name)
        except PackageNotFoundError:
            libraries[name] = None
    manifest = {
        "kind": "ml_research", "hypothesis": "dimensionless-60-lightgbm-v1",
        "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "interval_role": "review", "promotion_allowed": False,
        "input_files": inputs, "start": args.start_date, "end": args.end_date,
        "config": asdict(config), "execution": asdict(execution),
        "feature_version": FEATURE_VERSION, "features": FEATURE_COLS,
        "model_params": dict(MODEL_PARAMS, n_jobs=config.threads),
        "code_version": code_version(), "libraries": libraries,
        "benchmarks": ["fixed_momentum20_same_top_k", "510300", "510500", "cash"],
        "selection_rule": "全部结果保留；不根据本次回放挑选或调整策略",
    }
    directory.mkdir(parents=True)
    # 配置和输入指纹先落盘，之后才读取收益数据并执行实验。
    write_json(directory / "manifest.json", manifest)
    store_path = directory / "evidence.sqlite3"
    identity = register(manifest, store_path)
    started, cpu_start = time.monotonic(), time.process_time()
    try:
        panel, metadata, calendar, expected_codes = read_input(args.input, args.start_date, args.end_date)
        benchmarks, benchmark_metadata, benchmark_calendar, _ = read_input(args.benchmark_input, args.start_date, args.end_date)
        if benchmark_calendar is not None and calendar != benchmark_calendar:
            raise ValueError("股票与基准声明的独立交易日历不一致")
        report = run_ml_research(panel, benchmarks, config, execution, calendar=calendar,
                                 metadata=metadata, benchmark_metadata=benchmark_metadata,
                                 expected_codes=expected_codes,
                                 progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
        # 输入在读取过程中发生变更时作废，不把混合快照冒充可复现输入。
        if any(file_hash(item["file"]) != item["sha256"] for item in inputs.values()):
            raise ValueError("实验期间输入文件发生变化，请使用冻结快照重新运行")
        if code_version() != manifest["code_version"]:
            raise ValueError("实验期间源代码发生变化，请使用新目录重新运行")
        import pandas as pd
        predictions = report.pop("predictions", None)
        if predictions is not None:
            pd.DataFrame(predictions).to_csv(directory / "predictions.csv.gz", index=False, compression="gzip")
        accounts = report.pop("accounts", {})
        for name, book in accounts.items():
            write_json(directory / f"account-{name}.json", book)
        selections = report.pop("selections", {})
        write_json(directory / "selections.json", selections)
        report["resources"] = {"wall_seconds": round(time.monotonic() - started, 3),
                               "cpu_seconds": round(time.process_time() - cpu_start, 3),
                               "peak_rss_mib": peak_rss_mib(), "platform": sys.platform,
                               "python": sys.version.split()[0], "threads": config.threads}
        report["as_of"] = args.end_date
        write_json(directory / "report.json", report)
        (directory / "report.md").write_text(render_report(report), encoding="utf-8")
        state = "completed" if report["status"] == "completed" else "waiting_data"
        save_report(identity, report, store_path, state)
        summary = {k: report[k] for k in ("status", "execution_status", "metrics", "excess_total_return", "resources", "reasons") if k in report}
        summary["experiment_id"] = identity
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
