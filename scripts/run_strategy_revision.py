"""受限策略修订实验：先用2025段选择，再冻结评估2026，不自动晋级。"""
import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio.factor_backtest import ExecutionConfig, run_portfolio, summarize_curve
from strategy.diversified_research import DiversifiedConfig, build_targets
from scripts.run_sector_research import UNIVERSE, write_json


def window(book, start, end):
    earlier = [r for r in book["curve"] if r["date"] < start]
    capital = earlier[-1]["equity"] if earlier else book["config"]["capital"]
    curve = [r for r in book["curve"] if start <= r["date"] <= end]
    trades = [t for t in book["trades"] if start <= t["date"] <= end]
    return summarize_curve(curve, trades, capital)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/reports/kt-sector-20261011/panel.json")
    parser.add_argument("--output", default="data/reports/strategy-revision-20261011")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    input_path = Path(args.input)
    panel = pd.read_json(input_path, dtype={"code": str}, convert_dates=False)
    panel["date"] = pd.to_datetime(panel["date"])
    sectors = {code: sector for sector, members in UNIVERSE.items() for code in members}
    baseline = DiversifiedConfig(mode="equal", top_k=60, retention_rank=60,
                                sector_slots=6, max_weight=1/60, trend_filter=False)
    variants = {
        "monthly_equal": baseline,
        "broad_trend": replace(baseline, trend_filter=True),
        "momentum20": DiversifiedConfig(mode="momentum"),
        "balanced20": DiversifiedConfig(mode="balanced"),
    }
    execution = ExecutionConfig(capital=10_000_000, max_positions=60, max_weight=0.05, mode="research")
    manifest = {"input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                "signal_start": "2025-05-29", "selection_end": "2025-12-31", "evaluation_start": "2026-01-01",
                "variants": {k:asdict(v) for k,v in variants.items()}, "execution": asdict(execution),
                "selection_rule": "2025净收益减0.5倍最大回撤绝对值最大；包含等权对照，且胜者分数必须大于0，否则选现金",
                "caveat": "2026在前次实验已经被观察过，本次仅为受污染历史上的冻结方案检查，不是新的独立测试",
                "future_work": "需要未观察市场区间的模拟盘前向验证；不自动晋级"}
    write_json(root / "manifest.json", manifest)
    # 选择阶段物理截断到2025，避免统计路径误读2026。
    development = panel[panel.date <= "2025-12-31"].copy()
    selections = {}
    for name, config in variants.items():
        targets, audit = build_targets(development, sectors, "2025-05-29", config)
        book = run_portfolio(development, targets, replace(execution, max_positions=config.top_k, max_weight=config.max_weight))
        m = book["metrics"]
        selections[name] = {"metrics":m, "objective":m["total_return"] - 0.5 * abs(m["max_drawdown"])}
        write_json(root / (name + "-development.json"), {"book":book, "signals":audit})
        print("DEVELOPMENT", name, json.dumps(selections[name]), flush=True)
    selected = max(selections, key=lambda name: (selections[name]["objective"], name))
    if selections[selected]["objective"] <= 0:
        selected = "cash"
    write_json(root / "frozen_selection.json", {"selected":selected, "development":selections})
    print("FROZEN_SELECTION", selected, flush=True)
    results, books = {}, {}
    for name, config in variants.items():
        targets, audit = build_targets(panel, sectors, "2025-05-29", config)
        book = run_portfolio(panel, targets, replace(execution, max_positions=config.top_k, max_weight=config.max_weight))
        dev_signals = json.loads((root / (name+"-development.json")).read_text(encoding="utf-8"))["signals"]
        assert dev_signals == audit[:len(dev_signals)], "未来数据影响了开发期目标"
        development_book = json.loads((root / (name+"-development.json")).read_text(encoding="utf-8"))["book"]
        assert development_book["curve"] == [r for r in book["curve"] if r["date"] <= "2025-12-31"], "未来数据影响了开发期净值"
        books[name] = book
        write_json(root / (name + "-full.json"), {"book":book, "signals":audit})
        results[name] = {"total":book["metrics"], "2025":window(book,"2025-01-01","2025-12-31"),
                         "2026":window(book,"2026-01-01","2026-10-09"),
                         "average_exposure":float(np.mean([1-r["cash"]/r["equity"] for r in book["curve"]])),
                         "stale_mark_days":sum(bool(r["stale_marks"]) for r in book["curve"])}
        print("EVALUATION", name, json.dumps(results[name]), flush=True)
    # 原版无成本回放仅用于归因，不用它更改选择结果。
    original_path = input_path.parent / "pooled_ic_weighted.json"
    original = json.loads(original_path.read_text(encoding="utf-8"))
    original_targets = {pd.Timestamp(r["date"]):r["weights"] for r in original["selections"]}
    old_gross = run_portfolio(panel, original_targets, replace(execution, max_positions=5,max_weight=0.2,
                             commission=0, min_commission=0, stamp_tax=0, slippage=0))
    write_json(root / "original-zero-cost.json", old_gross)
    old = json.loads((input_path.parent / "pooled_ic_weighted-capacity.json").read_text(encoding="utf-8"))["strategy"]
    sensitivity = {}
    if selected != "cash":
        targets, _ = build_targets(panel, sectors, "2025-05-29", variants[selected])
        for capital, slip in [(100_000, .001), (10_000_000,.0005), (10_000_000,.002)]:
            book = run_portfolio(panel, targets, replace(execution, capital=capital, slippage=slip,
                                 max_positions=variants[selected].top_k, max_weight=variants[selected].max_weight))
            label = f"capital{capital}_slippage{slip}"
            write_json(root / (label+".json"), book)
            sensitivity[label] = {"total":book["metrics"], "2026":window(book,"2026-01-01","2026-10-09")}
    assessment = {
        "selected": selected,
        "beats_equal_total": selected != "cash" and results[selected]["total"]["total_return"] > results["monthly_equal"]["total"]["total_return"],
        "beats_equal_2026": selected != "cash" and results[selected]["2026"]["total_return"] > results["monthly_equal"]["2026"]["total_return"],
        "note": "描述性对照，不能将同样已观察的历史重新当作独立验证；不晋级",
    }
    summary = {"selected":selected,"assessment":assessment,"variants":results,"original_net":old["metrics"],
               "original_zero_cost":old_gross["metrics"],"sensitivity":sensitivity,"promotion_allowed":False}
    write_json(root / "summary.json", summary)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(11,5))
    for name, book in {"original":old, **books}.items():
        ax.plot(pd.to_datetime([r["date"] for r in book["curve"]]),
                [r["equity"]/book["config"]["capital"] for r in book["curve"]],label=name)
    ax.axvline(pd.Timestamp("2026-01-01"), linestyle="--",color="gray",label="Frozen evaluation boundary")
    ax.axhline(1,linestyle=":",color="gray")
    ax.set_title("Fixed research candidates / KT 60 stocks / historical replay")
    ax.set_ylabel("Equity / initial capital")
    ax.grid(alpha=.2)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(root/"equity.png",dpi=160)
    plt.close(fig)
    lines=["# 策略修订研究报告", "",f"开发期冻结选择：**{selected}**。所有候选完整保留，未按2026结果重选。",
           "", "相同KT 60股输入、近似成交和费用口径；资金1000万元降低整手误差，10万元另做敏感性。",
           "原版每5日调仓、5股集中、训练IC方向变化；候选改为20日调仓，固定中期动量方向，趋势筛选、分散持仓和排名缓冲。",
           "", "选择只读取截至2025-12-31数据：目标=收益-0.5×最大回撤绝对值，等权对照也参与选择，目标不正则现金。",
           "2026已在旧实验被查看过，仍属受污染历史研究；不宣称独立测试，不自动替换正式交易策略。", "",
           "|方案|全段收益|最大回撤|2025开发段|2026检查段|成交笔数|平均仓位|",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for name,item in results.items():
        m=item["total"]
        lines.append(f"|{name}|{m['total_return']:+.2%}|{m['max_drawdown']:.2%}|{item['2025']['total_return']:+.2%}|{item['2026']['total_return']:+.2%}|{m['trade_count']}|{item['average_exposure']:.1%}|")
    lines += ["",f"原版扣费收益 {old['metrics']['total_return']:+.2%}；零费用零滑点回放 {old_gross['metrics']['total_return']:+.2%}。",
              "零费用对照会因资金路径变化改变整手数量，不是简单的会计成本相减。", "", "![净值](equity.png)", "",
              "方案说明：monthly_equal为月度全池等权；broad_trend对全池等权增加MA120与跳过最近20日的中期动量>0筛选，未合格名额留现金；momentum20选中期动量前20；balanced20将动量与低波动秩等权融合。后两者行业最多4股、单股5%，原成员在前30名内优先保留。",
              "行业分组是人工研究分组，不是历史行业中性化；相同存续股票池选择偏差、前复权成交近似、缺行情旧价估值和公司行动限制继续适用。成本和资金敏感性详见 summary.json。", ""]
    lines += ["## 验收判读", "",
              f"冻结方案全段跑赢月度等权：{assessment['beats_equal_total']}；2026段跑赢月度等权：{assessment['beats_equal_2026']}。",
              "降低亏损或回撤不等于获得选股优势，低仓位方案尤其需要和现金/等风险对照比较。保留为研究候选，不部署。", ""]
    (root/"report.md").write_text("\n".join(lines),encoding="utf-8")
    print("COMPLETE",root/"report.md",flush=True)


if __name__ == "__main__":
    main()
