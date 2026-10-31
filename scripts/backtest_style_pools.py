"""风格池与因子分的历史滚动回测（只读复权日线库，不下单、不写库）。

用法：
    python scripts/backtest_style_pools.py [--top-n 5 15] [--hold 20] [--start 2017-11-01]

口径：
- 每 hold 个交易日调仓一次；t 日收盘按 strategy.style_pools 选股，t+1 开盘等权买入，
  持有 hold 日后在开盘卖出；t+1 开盘已涨停（无法买入）的标的剔除；扣双边成本。
- 因子权重在每个调仓日用“截至当日标签已可见”的窗口重新估计（滚动样本外）。
- 基本面质量池需要历史财务快照，历史回测不可得，不参与。
- “+大盘上限”变体按全市场等权指数的双通道状态缩放仓位（其余持现金）。
- 股票池为当前数据包覆盖的股票，可能存在幸存者偏差；结论应看与等权基准的相对差。
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import adjusted_daily  # noqa: E402
from strategy import style_pools as sp  # noqa: E402
from strategy.style_shadow import ROUND_TRIP_COST  # noqa: E402
from strategy.trend_channel import exposure_cap  # noqa: E402

PERIODS = {"全期": (None, None), "2017-2021": ("2017-01-01", "2021-12-31"), "2022-2026": ("2022-01-01", "2026-12-31")}


def _metrics(returns: pd.Series, hold: int) -> dict:
    returns = returns.dropna()
    if len(returns) < 4:
        return {}
    per_year = 244 / hold
    equity = (1 + returns).cumprod()
    years = len(returns) / per_year
    cagr = equity.iloc[-1] ** (1 / years) - 1
    drawdown = float((equity / equity.cummax() - 1).min())
    sharpe = returns.mean() / returns.std() * np.sqrt(per_year) if returns.std() > 0 else float("nan")
    return {"cagr": float(cagr), "maxdd": drawdown, "sharpe": float(sharpe), "periods": int(len(returns))}


def run(top_ns=(5, 15), hold=20, start="2017-11-01", db_path=None):
    panel = adjusted_daily.load_panel("2016-01-01", db_path=db_path)
    if not panel:
        raise SystemExit("复权日线库为空，请先运行 python main.py --adjusted-daily-sync")
    factors = sp.compute_factors(panel)
    universe = sp.universe_mask(panel, factors)
    dates = list(panel["close"].index)
    fwd = sp.forward_returns(panel, hold)
    ic_fwd = sp.forward_returns(panel, sp.ICConfig().horizon)
    limit = panel["raw_open"] * 0 + 0.1
    limit.loc[:, [c for c in limit.columns if c.startswith("30")]] = 0.1
    limit.loc[limit.index >= "2020-08-24", [c for c in limit.columns if c.startswith("30")]] = 0.2
    up_lock = (panel["raw_open"] >= (panel["ref"] * (1 + limit)).round(2) - 0.005).shift(-1).fillna(False)

    market = panel["close"].pct_change(fill_method=None).where(universe.shift(1).fillna(False)).mean(axis=1).fillna(0)
    market_index = (1 + market).cumprod()
    cap = exposure_cap(market_index * 1.0, market_index * 1.0, market_index)

    rebalance = [i for i in range(dates.index(next(d for d in dates if d >= start)), len(dates) - hold - 1, hold)]
    rows = []
    for pos in rebalance:
        date = dates[pos]
        weights = sp.ic_weights(factors, universe, ic_fwd, pos)
        scores = sp.factor_scores(factors, universe, date, weights)
        pools = sp.select_pools(factors, universe, date, top_n=max(top_ns), scores=scores)
        bench = fwd.loc[date][universe.loc[date].fillna(False).astype(bool)].mean() - ROUND_TRIP_COST
        row = {"date": date, "universe_ew": bench, "cap": float(cap.loc[date])}
        for style, frame in pools.items():
            if frame.empty:
                continue
            for n in top_ns:
                codes = [c for c in frame["code"].head(n) if not up_lock.loc[date].get(c, False)]
                values = fwd.loc[date, codes].dropna() if codes else pd.Series(dtype=float)
                ret = float(values.mean()) - ROUND_TRIP_COST if len(values) else 0.0
                row[f"{style}@{n}"] = ret
                row[f"{style}@{n}+大盘上限"] = ret * row["cap"]
        rows.append(row)
    table = pd.DataFrame(rows).set_index("date")
    report = {}
    for column in [c for c in table.columns if c != "cap"]:
        report[column] = {}
        for name, (a, b) in PERIODS.items():
            part = table[column].loc[a or table.index[0]:b or table.index[-1]]
            m = _metrics(part, hold)
            bench = table["universe_ew"].loc[part.index]
            if m and column != "universe_ew":
                m["beat_ew_ratio"] = float((part > bench).mean())
                m["mean_excess"] = float((part - bench).mean())
            report[column][name] = m
    return report, table


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-n", type=int, nargs="+", default=[5, 15])
    parser.add_argument("--hold", type=int, default=20)
    parser.add_argument("--start", default="2017-11-01")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report, table = run(tuple(args.top_n), args.hold, args.start)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"调仓 {len(table)} 次，每 {args.hold} 个交易日；扣双边成本 {ROUND_TRIP_COST:.2%}")
    for column, periods in report.items():
        parts = []
        for name, m in periods.items():
            if not m:
                continue
            extra = f" 胜等权{m['beat_ew_ratio']:.0%} 超额{m['mean_excess']*100:+.2f}%/期" if "beat_ew_ratio" in m else ""
            parts.append(f"{name}: 年化{m['cagr']*100:+.1f}% 回撤{m['maxdd']*100:.1f}% Sharpe{m['sharpe']:.2f}{extra}")
        print(f"{column:32s} " + " | ".join(parts))


if __name__ == "__main__":
    main()
