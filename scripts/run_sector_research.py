"""KT 日线跨行业研究实验。独立目录保存输入、预设配置和全部结果，不改正式数据库。"""
import argparse
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.kt_realtime import get_kt_client
from portfolio.factor_backtest import ExecutionConfig, run_portfolio
from strategy.factor_research import FactorConfig, run_factor_research


# 运行前固定的人工行业分组，不宣称是历史指数成分或行业分类数据库。
UNIVERSE = {
    "银行": {"600036": "招商银行", "601398": "工商银行", "601939": "建设银行", "601288": "农业银行", "601166": "兴业银行", "000001": "平安银行"},
    "证券保险": {"601318": "中国平安", "601601": "中国太保", "601628": "中国人寿", "600030": "中信证券", "601688": "华泰证券", "600999": "招商证券"},
    "食品饮料": {"600519": "贵州茅台", "000858": "五粮液", "000568": "泸州老窖", "600887": "伊利股份", "600600": "青岛啤酒", "603288": "海天味业"},
    "家电": {"000333": "美的集团", "000651": "格力电器", "600690": "海尔智家", "000921": "海信家电", "002032": "苏泊尔", "002050": "三花智控"},
    "医药": {"600276": "恒瑞医药", "000538": "云南白药", "600436": "片仔癀", "600196": "复星医药", "600085": "同仁堂", "000423": "东阿阿胶"},
    "电子通信": {"002415": "海康威视", "002475": "立讯精密", "002371": "北方华创", "603501": "韦尔股份", "000725": "京东方A", "000063": "中兴通讯"},
    "汽车零部件": {"002594": "比亚迪", "600104": "上汽集团", "000625": "长安汽车", "601633": "长城汽车", "601238": "广汽集团", "600660": "福耀玻璃"},
    "能源电力": {"601088": "中国神华", "600900": "长江电力", "601857": "中国石油", "600028": "中国石化", "600011": "华能国际", "601225": "陕西煤业"},
    "有色化工": {"601899": "紫金矿业", "600547": "山东黄金", "603993": "洛阳钼业", "600309": "万华化学", "002460": "赣锋锂业", "002466": "天齐锂业"},
    "基建运输": {"601668": "中国建筑", "601390": "中国中铁", "601186": "中国铁建", "600009": "上海机场", "601006": "大秦铁路", "600029": "南方航空"},
}


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)


def collect(directory, end_date):
    """有界串行获取公开 KT 行情，保留每只股票来源/覆盖及原始适配结果。"""
    rows, coverage = [], []
    client = get_kt_client()
    for sector, members in UNIVERSE.items():
        for code, name in members.items():
            frame = pd.DataFrame()
            for attempt in range(2):
                frame = client.get_kline(code, period="d", count=1000)
                if not frame.empty:
                    break
                time.sleep(1)
            if frame.empty or frame.attrs.get("adjustment") != "qfq":
                raise ValueError(f"{code} 未返回前复权日线；不静默删除股票或切换数据源")
            frame = frame[frame["data_date"] <= end_date].sort_values("data_date").copy()
            if frame.empty or frame["data_date"].iloc[-1] != end_date:
                raise ValueError(f"{code} 没有覆盖所需终点 {end_date}")
            if frame["data_date"].duplicated().any():
                raise ValueError(f"{code} 返回重复日期")
            output = pd.DataFrame({
                "date": frame["data_date"], "code": code,
                "open": frame["open"], "high": frame["high"],
                "low": frame["low"], "close": frame["close"],
                "volume": frame["volume_raw"], "source": "kt_tencent_qfq",
                "research_sector": sector,
            })
            if not np.isfinite(output[["open", "high", "low", "close", "volume"]]).all().all():
                raise ValueError(f"{code} 非有限量价")
            raw_path = directory / (code + "-kt.json")
            raw_path.write_text(frame.to_json(orient="records", force_ascii=False), encoding="utf-8")
            coverage.append({"code": code, "name": name, "sector": sector, "rows": len(output),
                             "start": output["date"].iloc[0], "end": output["date"].iloc[-1],
                             "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest()})
            rows.append(output)
            print(f"DATA {len(rows)}/60 {code} {name}: {len(output)} rows", flush=True)
            time.sleep(0.15)
    panel = pd.concat(rows, ignore_index=True).sort_values(["date", "code"])
    # 固定同一开始日期，避免较长历史股票天然得到更多训练数据。
    common_start = max(row["start"] for row in coverage)
    panel = panel[panel["date"] >= common_start].copy()
    all_dates = set(panel["date"])
    for entry in coverage:
        dates = set(panel.loc[panel["code"] == entry["code"], "date"])
        entry["missing_panel_dates"] = sorted(all_dates - dates)
    write_json(directory / "coverage.json", {"common_start": common_start, "end": end_date,
                                            "panel_dates": len(all_dates), "stocks": coverage})
    panel.to_json(directory / "panel.json", orient="records", force_ascii=False)
    return panel


def compact_report(report):
    return {"execution_status": report["execution_status"],
            "strategy": report["strategy"]["metrics"],
            "benchmark": report["equal_weight_benchmark"]["metrics"],
            "excess_return": report["excess_total_return"],
            "validation_summary": report["validation_summary"],
            "first_execution": report["strategy"]["curve"][0]["date"],
            "last_execution": report["strategy"]["curve"][-1]["date"],
            "blocked_counts": report["strategy"]["blocked_counts"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--end-date", default="2026-10-09")
    parser.add_argument("--reuse-panel", action="store_true", help="只复用本目录已固定输入，不重新下载")
    args = parser.parse_args()
    directory = Path(args.output).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    config = FactorConfig(train_days=250, valid_days=60, rebalance_days=5,
                          min_stocks=20, top_k=5)
    execution = ExecutionConfig(capital=100_000, max_positions=5, max_weight=0.2, mode="research")
    manifest = {"created_at": datetime.now().isoformat(), "requested_end": args.end_date,
                "universe": UNIVERSE, "config": asdict(config), "execution": asdict(execution),
                "prespecified_comparisons": ["pooled_ic_weighted", "pooled_equal", "10 sector top3 portfolios",
                                             "pooled IC targets at 0.05%/0.1%/0.2% per-side slippage"],
                "selection_bias": "人工当前存续主板大中盘股票池，不含退市股票；分组不是历史时点行业数据库",
                "price_model": "统一 KT 前复权量价的近似成交；没有公司行动、历史ST/涨跌停元数据，不是精确执行模拟"}
    if not args.reuse_panel:
        write_json(directory / "manifest.json", manifest)
        panel = collect(directory, args.end_date)
    else:
        stored = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if any(stored[key] != manifest[key] for key in ("requested_end", "universe", "config", "execution")):
            raise ValueError("已固定的实验配置与当前参数不同，请使用新的实验目录")
        panel = pd.read_json(directory / "panel.json", dtype={"code": str}, convert_dates=False)
    summaries = {}
    for fusion in ("ic_weighted", "equal"):
        key = "pooled_" + fusion
        report_path = directory / (key + ".json")
        print("RUN " + key, flush=True)
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
        else:
            report = run_factor_research(panel, replace(config, fusion=fusion), execution)
            write_json(report_path, report)
        summaries[key] = compact_report(report)
        if fusion == "ic_weighted":
            targets = {pd.Timestamp(row["date"]): row["weights"] for row in report["selections"]}
            execution_panel = panel.copy()
            execution_panel["date"] = pd.to_datetime(execution_panel["date"])
            sensitivity = {}
            for slippage in (0.0005, 0.001, 0.002):
                book = run_portfolio(execution_panel, targets, replace(execution, slippage=slippage))
                sensitivity[str(slippage)] = book["metrics"]
            summaries["slippage_sensitivity"] = sensitivity
        print(json.dumps({key: summaries[key]}, ensure_ascii=False), flush=True)
    for index, (sector, members) in enumerate(UNIVERSE.items(), 1):
        key = f"sector_{index:02d}"
        print("RUN " + sector, flush=True)
        report_path = directory / (key + ".json")
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
        else:
            report = run_factor_research(panel[panel["code"].isin(members)].copy(),
                                        replace(config, min_stocks=5, top_k=3),
                                        replace(execution, max_positions=3, max_weight=1 / 3))
            write_json(report_path, report)
        summaries[sector] = compact_report(report)
        print(json.dumps({sector: summaries[sector]}, ensure_ascii=False), flush=True)
    write_json(directory / "summary.json", summaries)
    print("COMPLETE " + str(directory / "summary.json"), flush=True)


if __name__ == "__main__":
    main()
