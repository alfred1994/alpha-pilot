"""汇总固定行业实验，并以相同信号回放更大资金，分离整手资金约束的影响。"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portfolio.factor_backtest import ExecutionConfig, run_portfolio
from scripts.run_sector_research import UNIVERSE, write_json


def exposure(book):
    return float(np.mean([1 - row["cash"] / row["equity"] for row in book["curve"]]))


def period_return(book, start, end, capital):
    earlier = [row for row in book["curve"] if row["date"] < start]
    selected = [row for row in book["curve"] if start <= row["date"] <= end]
    base = earlier[-1]["equity"] if earlier else capital
    return selected[-1]["equity"] / base - 1 if selected else None


def capacity_replay(panel, report, capital):
    """不重训、不改变因子或选择，仅改资金规模。"""
    config = ExecutionConfig(**report["strategy"]["config"])
    config = replace(config, capital=capital)
    targets = {pd.Timestamp(row["date"]): row["weights"] for row in report["selections"]}
    # 原始对照包含信号日具备完整因子的股票；无有效选中因子时排名为空，
    # 不能由 ranking 还原对照成员，必须使用同一特征计算规则。
    from strategy.factor_research import build_factor_panel, validate_panel
    factors = build_factor_panel(validate_panel(panel), report["config"]["rebalance_days"], report["config"]["neutralize"])
    benchmark_targets = {}
    for date in targets:
        cross = factors[factors["date"] == date].dropna(subset=report["factors"])
        codes = cross["code"].tolist() if len(cross) >= report["config"]["min_stocks"] else []
        benchmark_targets[date] = {code: min(1 / len(codes), config.max_weight) for code in codes}
    return {
        "capital": capital, "same_signal_replay": True,
        "strategy": run_portfolio(panel, targets, config),
        "benchmark": run_portfolio(panel, benchmark_targets, replace(config, max_positions=panel.code.nunique())),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--capital", type=float, default=10_000_000)
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    panel = pd.read_json(root / "panel.json", dtype={"code": str}, convert_dates=False)
    panel["date"] = pd.to_datetime(panel["date"])
    comparisons = [("pooled_ic_weighted", "全池 RankIC 加权", panel), ("pooled_equal", "全池因子等权", panel)]
    comparisons += [(f"sector_{i:02d}", sector, panel[panel.code.isin(members)].copy())
                    for i, (sector, members) in enumerate(UNIVERSE.items(), 1)]
    summaries, books = {}, {}
    for key, label, subset in comparisons:
        print("REPLAY " + label, flush=True)
        source = root / (key + ".json")
        report = json.loads(source.read_text(encoding="utf-8"))
        dest = root / (key + "-capacity.json")
        if dest.exists():
            replay = json.loads(dest.read_text(encoding="utf-8"))
            if replay["capital"] != args.capital:
                raise ValueError("已存在不同资金规模的回放，不覆盖")
        else:
            replay = capacity_replay(subset, report, args.capital)
            write_json(dest, replay)
        books[key] = replay
        summaries[label] = {
            "strategy": replay["strategy"]["metrics"], "benchmark": replay["benchmark"]["metrics"],
            "strategy_exposure": exposure(replay["strategy"]), "benchmark_exposure": exposure(replay["benchmark"]),
            "excess_return": replay["strategy"]["metrics"]["total_return"] - replay["benchmark"]["metrics"]["total_return"],
            "stale_valuation_days": {name: sum(bool(row["stale_marks"]) for row in replay[name]["curve"])
                                     for name in ("strategy", "benchmark")},
            "periods": {year: {name: period_return(replay[name], start, end, args.capital) for name in ("strategy", "benchmark")}
                        for year, start, end in [("2025样本外", "2025-01-01", "2025-12-31"),
                                                ("2026截至10月9日", "2026-01-01", "2026-10-09")]},
            "selected_factor_folds": report["validation_summary"]["folds_with_selected_factors"],
        }
    # 同一输入、同一初始信号，在固定成本档位下回放；不按结果选择费率。
    report = json.loads((root / "pooled_ic_weighted.json").read_text(encoding="utf-8"))
    targets = {pd.Timestamp(row["date"]): row["weights"] for row in report["selections"]}
    config = replace(ExecutionConfig(**report["strategy"]["config"]), capital=args.capital)
    sensitivity = {}
    for slippage in (0.0005, 0.001, 0.002):
        sensitivity[str(slippage)] = run_portfolio(panel, targets, replace(config, slippage=slippage))["metrics"]
    write_json(root / "capacity-summary.json", {"capital": args.capital, "results": summaries, "slippage_sensitivity": sensitivity})

    # 用未重新缩放的净值绘图；图中的现金线明确为零收益。
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    for key, name, field in [("pooled_ic_weighted", "RankIC-weighted factors", "strategy"),
                             ("pooled_equal", "Equal-weighted factors", "strategy"),
                             ("pooled_ic_weighted", "Universe equal-weight benchmark", "benchmark")]:
        curve = books[key][field]["curve"]
        dates = pd.to_datetime([row["date"] for row in curve])
        equity = np.array([row["equity"] / args.capital for row in curve])
        axes[0].plot(dates, equity, label=name)
        peak = np.maximum.accumulate(np.r_[1.0, equity])[1:]
        axes[1].plot(dates, (equity / peak - 1) * 100, label=name)
    axes[0].axhline(1.0, color="gray", linestyle="--", label="Cash")
    axes[0].set_title("KT / 60 stocks / OOS research simulation through 2026-10-09")
    axes[0].set_ylabel("Equity / starting capital")
    axes[0].legend(fontsize=9)
    axes[1].set_ylabel("Drawdown (%)")
    for axis in axes:
        axis.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(root / "equity.png", dpi=160)
    plt.close(fig)

    rows = ["# KT 跨行业滚动回测报告", "",
            "本报告是固定参数、公开 KT 前复权行情的研究模拟，不是实盘或可保证成交的业绩。", "",
            "## 范围与方法", "",
            "- 股票池：10 个人工行业组，每组 6 只存续主板股票，共 60 只；代码名单见 manifest.json。非全市场随机样本，存在规模、存续与人为选样偏差。",
            "- 原始行情：2024-02-19 至 2026-10-09；640 个面板日期，38,380 根日线，全部 KT 腾讯前复权，不混用 BaoStock。",
            "- 样本外交易：2025-05-30 至 2026-10-09；之前仅用于指标预热和训练。250 日训练、60 日验证、5 日调仓，共 6 折。",
            "- 全池选前 5 只，行业内选前 3 只；因子筛选、方向和权重全部由当时训练窗决定，未查看结果后优化。",
            "- 对照为同池等权定期调仓组合。两种因子融合都评估，不以表现好者作为最终独立测试结论。",
            f"- 下表资金规模为 {args.capital:,.0f} 元，同信号回放；另保留 10 万元原始报告，以检查整手和最低佣金的影响。",
            "- 默认每边佣金0.03%、最低5元；卖出印花税0.05%，每边滑点0.1%。期末为持仓市值，未扣未来平仓费。", "",
            "## 结果", "",
            "|范围|策略收益|同池等权收益|超额（百分点）|策略最大回撤|策略平均仓位|对照平均仓位|成交笔数|",
            "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for label, item in summaries.items():
        a, b = item["strategy"], item["benchmark"]
        rows.append(f"|{label}|{a['total_return']:+.2%}|{b['total_return']:+.2%}|{item['excess_return']*100:+.2f}|{a['max_drawdown']:.2%}|{item['strategy_exposure']:.1%}|{item['benchmark_exposure']:.1%}|{a['trade_count']}|")
    rows += ["", "![净值与回撤](equity.png)", "", "## 全池分年度结果", "",
             "|组合|期间|策略收益|同池等权收益|", "|---|---|---:|---:|"]
    for label in ("全池 RankIC 加权", "全池因子等权"):
        for period, values in summaries[label]["periods"].items():
            rows.append(f"|{label}|{period}|{values['strategy']:+.2%}|{values['benchmark']:+.2%}|")
    rows += ["", "## 固定信号滑点敏感性", "", "|每边滑点|全池 RankIC 扣费收益|", "|---|---:|"]
    for slippage, metrics in sensitivity.items():
        rows.append(f"|{float(slippage):.2%}|{metrics['total_return']:+.2%}|")
    rows += ["", "## 关键限制", "",
             "1. 10万元分散60只股票时买不起大量高价股一手，等权对照平均仓位仅约23%；因此追加1000万元同信号回放检验资金可分配性，不根据结果调整信号。两组结果都保留。",
             "2. 广汽集团在2026-09-14至09-28的10个面板日期缺行情，中国神华在2025-08-04至08-15的10个日期缺行情；未外部确认是否停牌，不插值、缺行情不成交。存量持仓按最后价估值并记录 stale_marks，期间回撤可能低估。",
             "3. 前复权价格适合研究收益，但其绝对价格会影响模拟整手与最低佣金；未完整模拟分红送转、历史ST、真实涨跌停价和集合竞价成交概率。10%涨跌停为普通主板近似。",
             "4. 当前行业分组仅用于分层采样，不用于回填历史行业标签；没有可靠历史市值，未做行业/市值中性化。",
             "5. 多日标签有重叠，IC门槛不是统计显著性；行业内仅6只股票，横截面因子估计更不稳定。未进行多重检验校正。",
             "6. 这是滚动样本外研究，末段也已查看，不能再当成未触碰的最终留出期。没有调参后重测或自动晋级。", "",
             "## 文件", "",
             "- manifest.json：下载前固定的股票池和实验参数。",
             "- coverage.json / panel.json / *-kt.json：覆盖检查、统一面板和适配器原始返回快照。",
             "- pooled_*.json / sector_*.json：原始10万元报告；*-capacity.json 为同信号1000万元回放。",
             "- summary.json / capacity-summary.json：两种资金规模汇总、分年度结果与费用敏感性。", ""]
    (root / "report.md").write_text("\n".join(rows), encoding="utf-8")
    print("COMPLETE " + str(root / "report.md"), flush=True)


if __name__ == "__main__":
    main()
