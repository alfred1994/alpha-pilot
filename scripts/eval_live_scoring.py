"""线上技术分（9 指标均值）与买入门槛的历史检验（只读复权日线库，不下单、不写库）。

线上 `strategy.decision._technical_dimension` 对 9 个技术子信号取简单平均，
`scheduler.pipeline.select_llm_candidates` 要求技术分 ≥ DECISION_MIN_BUY_TECHNICAL(58)
才进入 LLM。本脚本在全市场历史截面上重放同一套子信号，检验：

1. 技术分及每个子信号对未来扣费收益的排名 IC（逐日横截面，再取均值/ICIR/t 值）；
2. 技术分分档的平均净收益、相对同日可交易池等权的超额与胜率；
3. 门槛（默认 58）过线 vs 未过线的超额差；
4. 同一样本上的参照因子（低波动、中期反转、避开暴涨），便于和风格池结论对比。

口径：
- t 日收盘出信号，t+1 开盘买入，持有 h 日后开盘卖出，扣双边成本 ROUND_TRIP_COST；
  t+1 开盘涨停（买不进）的样本剔除。
- 股票池与风格池一致：上市满 250 日、20 日均成交额≥1 亿、价格≥3 元、近 20 日无异常日。
- 每隔 --step 个交易日取一个信号日，每日随机抽 --per-day 只（固定种子），控制运行时间。
- 输入是等比复权日线；线上 `k_daily` 为等差前复权，百分比类子指标在早年会有差异，
  结论应看方向与相对差，不看绝对数值。
- 市场状态用全市场等权指数近似：近 20 日下跌且低于 60 日均线记为 weak，其余为 other。
  这不是线上 market_regimes 的判定，仅用于分组。
- 股票池为当前数据包覆盖的股票，存在幸存者偏差。

用法（服务器，建议非交易时段或 nice 运行）：
    python scripts/eval_live_scoring.py [--start 2017-01-01] [--step 20] [--per-day 200]
        [--horizons 5 20] [--gate 58] [--json out.json]
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import adjusted_daily  # noqa: E402
from signals.technical import all_technical_signals  # noqa: E402
from strategy import style_pools as sp  # noqa: E402
from strategy.style_shadow import ROUND_TRIP_COST  # noqa: E402

WINDOW = 260  # 子信号最长需要约 125 根（Fibonacci 120 日窗口 + 5），留足余量
TECH_BUCKETS = (45, 50, 55, 58, 62)
REFERENCE_FACTORS = ("low_vol20", "reversal20", "anti_lottery20")
PERIODS = {"2017-2021": ("2017-01-01", "2021-12-31"), "2022-2026": ("2022-01-01", "2026-12-31")}


def technical_score(df: pd.DataFrame):
    """与线上 _technical_dimension 相同的聚合：只平均置信度>0 的子信号。"""
    signals = all_technical_signals(df)
    subs = {}
    available = []
    for item in signals:
        ok = math.isfinite(item.confidence) and item.confidence > 0 and math.isfinite(item.score)
        subs[item.name] = float(item.score) if ok else np.nan
        if ok:
            available.append(item)
    if not available:
        return np.nan, subs
    return float(sum(s.score for s in available) / len(available)), subs


def limit_up_open(panel: pd.DataFrame) -> pd.DataFrame:
    """t+1 开盘即涨停（买不进）标记，对齐到信号日 t。"""
    limit = panel["raw_open"] * 0 + 0.1
    chinext = [c for c in limit.columns if c.startswith("30")]
    limit.loc[limit.index >= adjusted_daily.CHINEXT_20PCT_SINCE, chinext] = 0.2
    locked = panel["raw_open"] >= (panel["ref"] * (1 + limit)).round(2) - 0.005
    return locked.shift(-1).fillna(False).astype(bool)


def market_state(panel: pd.DataFrame, universe: pd.DataFrame) -> pd.Series:
    daily = panel["close"].pct_change(fill_method=None).where(universe.shift(1).fillna(False).astype(bool))
    index = (1 + daily.mean(axis=1).fillna(0)).cumprod()
    weak = (index / index.shift(20) - 1 < 0) & (index < index.rolling(60).mean())
    return weak.map({True: "weak", False: "other"})


def collect(panel, *, start, step, per_day, horizons, seed=7, log_every=10):
    factors = sp.compute_factors(panel)
    universe = sp.universe_mask(panel, factors)
    locked = limit_up_open(panel)
    state = market_state(panel, universe)
    fwd = {h: sp.forward_returns(panel, h) - ROUND_TRIP_COST for h in horizons}
    dates = list(panel["close"].index)
    last_valid = len(dates) - max(horizons) - 2
    first = next(i for i, d in enumerate(dates) if d >= start and i >= WINDOW)
    positions = list(range(first, last_valid + 1, step))
    rng = np.random.default_rng(seed)
    columns = {name: panel[name] for name in ("open", "high", "low", "close", "volume")}

    rows = []
    started = time.time()
    for n, pos in enumerate(positions, start=1):
        date = dates[pos]
        mask = universe.iloc[pos].fillna(False).astype(bool) & ~locked.iloc[pos]
        codes = list(mask.index[mask])
        if not codes:
            continue
        # 当日等权基准用整个可交易池，抽样只决定打分对象
        bench = {h: float(fwd[h].iloc[pos][codes].mean()) for h in horizons}
        sample = rng.choice(codes, size=min(per_day, len(codes)), replace=False)
        lo = pos - WINDOW + 1
        block = {name: frame.iloc[lo:pos + 1] for name, frame in columns.items()}
        for code in sample:
            df = pd.DataFrame({name: block[name][code].to_numpy() for name in block})
            df.insert(0, "date", block["close"].index)
            df = df.dropna()
            if len(df) <= 30:
                continue
            score, subs = technical_score(df.reset_index(drop=True))
            row = {"date": date, "code": code, "state": state.iloc[pos], "tech": score, **subs}
            for name in REFERENCE_FACTORS:
                row[name] = float(factors[name].iloc[pos][code])
            for h in horizons:
                value = fwd[h].iloc[pos][code]
                row[f"net{h}"] = float(value) if pd.notna(value) else np.nan
                row[f"ex{h}"] = row[f"net{h}"] - bench[h] if pd.notna(value) else np.nan
            rows.append(row)
        if n % log_every == 0 or n == len(positions):
            print(f"[{n}/{len(positions)}] {date} rows={len(rows)} elapsed={time.time() - started:.0f}s",
                  file=sys.stderr, flush=True)
    return pd.DataFrame(rows)


def rank_ic(frame: pd.DataFrame, column: str, target: str, min_stocks: int = 30) -> dict:
    ics = []
    for _, group in frame.groupby("date"):
        group = group[[column, target]].dropna()
        if len(group) < min_stocks or group[column].nunique() < 2:
            continue
        ics.append(group[column].rank().corr(group[target].rank()))
    ics = pd.Series(ics, dtype=float).dropna()
    if len(ics) < 3:
        return {"days": int(len(ics)), "ic": None}
    mean, std = float(ics.mean()), float(ics.std(ddof=1))
    return {"days": int(len(ics)), "ic": round(mean, 4),
            "icir": round(mean / std, 3) if std > 0 else None,
            "t": round(mean / std * math.sqrt(len(ics)), 2) if std > 0 else None,
            "positive_days": round(float((ics > 0).mean()), 3)}


def bucket_table(frame: pd.DataFrame, horizon: int, edges=TECH_BUCKETS) -> list:
    labels = [f"<{edges[0]}"] + [f"{a}-{b}" for a, b in zip(edges, edges[1:])] + [f">={edges[-1]}"]
    cut = pd.cut(frame["tech"], bins=[-np.inf, *edges, np.inf], right=False, labels=labels)
    out = []
    for label, group in frame.groupby(cut, observed=False):
        group = group.dropna(subset=[f"net{horizon}"])
        if group.empty:
            out.append({"bucket": str(label), "n": 0})
            continue
        out.append({"bucket": str(label), "n": int(len(group)),
                    "share": round(len(group) / max(1, frame["tech"].notna().sum()), 3),
                    "mean_net": round(float(group[f"net{horizon}"].mean()), 5),
                    "mean_excess": round(float(group[f"ex{horizon}"].mean()), 5),
                    "win_rate": round(float((group[f"net{horizon}"] > 0).mean()), 3)})
    return out


def gate_split(frame: pd.DataFrame, horizon: int, gate: float) -> dict:
    data = frame.dropna(subset=["tech", f"ex{horizon}"])
    passed, failed = data[data["tech"] >= gate], data[data["tech"] < gate]

    def summary(group):
        if group.empty:
            return {"n": 0}
        daily = group.groupby("date")[f"ex{horizon}"].mean()
        return {"n": int(len(group)), "mean_net": round(float(group[f"net{horizon}"].mean()), 5),
                "mean_excess": round(float(group[f"ex{horizon}"].mean()), 5),
                "win_rate": round(float((group[f"net{horizon}"] > 0).mean()), 3),
                "days_beating_universe": round(float((daily > 0).mean()), 3)}

    return {"gate": gate, "pass_rate": round(len(passed) / max(1, len(data)), 3),
            "passed": summary(passed), "failed": summary(failed)}


def analyze(frame: pd.DataFrame, horizons, gate: float) -> dict:
    sub_names = [c for c in frame.columns if c not in {
        "date", "code", "state", "tech", *REFERENCE_FACTORS} and not c.startswith(("net", "ex"))]
    tech = frame["tech"].dropna()
    report = {
        "samples": int(len(frame)), "dates": int(frame["date"].nunique()),
        "range": [str(frame["date"].min()), str(frame["date"].max())],
        "tech_distribution": {"mean": round(float(tech.mean()), 2), "std": round(float(tech.std()), 2),
                              "p10": round(float(tech.quantile(0.1)), 2), "p50": round(float(tech.median()), 2),
                              "p90": round(float(tech.quantile(0.9)), 2)},
        "horizons": {},
    }
    slices = {"all": frame}
    for name, (lo, hi) in PERIODS.items():
        slices[name] = frame[(frame["date"] >= lo) & (frame["date"] <= hi)]
    for state in ("weak", "other"):
        slices[f"market_{state}"] = frame[frame["state"] == state]
    for h in horizons:
        target = f"ex{h}"
        block = {"ic": {}, "buckets": bucket_table(frame, h), "gate": {}}
        for label, part in slices.items():
            if part.empty:
                continue
            block["ic"][label] = {col: rank_ic(part, col, target) for col in ["tech", *sub_names, *REFERENCE_FACTORS]}
            block["gate"][label] = gate_split(part, h, gate)
        report["horizons"][f"{h}d"] = block
    return report


def format_report(report: dict) -> str:
    lines = [f"样本 {report['samples']} 条 / {report['dates']} 个信号日 / {report['range'][0]}~{report['range'][1]}",
             "技术分分布: " + json.dumps(report["tech_distribution"], ensure_ascii=False)]
    for horizon, block in report["horizons"].items():
        lines.append(f"\n== {horizon}（相对同日可交易池等权超额的排名 IC） ==")
        for label, ics in block["ic"].items():
            parts = [f"{col}={v['ic']:+.3f}(t={v['t']})" for col, v in ics.items() if v.get("ic") is not None]
            lines.append(f"[{label}] " + " ".join(parts))
        lines.append(f"-- 技术分分档（{horizon}） --")
        for row in block["buckets"]:
            if row.get("n"):
                lines.append(f"  {row['bucket']:>6}: n={row['n']} 占比{row['share']:.0%} 净{row['mean_net']*100:+.2f}% "
                             f"超额{row['mean_excess']*100:+.2f}% 胜率{row['win_rate']:.0%}")
        lines.append(f"-- 门槛过线 vs 未过线（{horizon}） --")
        for label, gate in block["gate"].items():
            p, f = gate["passed"], gate["failed"]
            if p.get("n") and f.get("n"):
                lines.append(f"  [{label}] 过线率{gate['pass_rate']:.0%} | 过线 超额{p['mean_excess']*100:+.2f}% "
                             f"胜率{p['win_rate']:.0%} | 未过线 超额{f['mean_excess']*100:+.2f}% 胜率{f['win_rate']:.0%}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--start", default="2017-01-01")
    parser.add_argument("--step", type=int, default=20)
    parser.add_argument("--per-day", type=int, default=200)
    parser.add_argument("--horizons", type=int, nargs="+", default=[5, 20])
    parser.add_argument("--gate", type=float, default=58.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--db", default=None, help="复权日线库路径，默认 ADJUSTED_DAILY_DB")
    parser.add_argument("--json", default=None, help="把完整结果写入该 JSON 文件")
    args = parser.parse_args(argv)

    load_from = (pd.Timestamp(args.start) - pd.Timedelta(days=500)).strftime("%Y-%m-%d")
    panel = adjusted_daily.load_panel(load_from, db_path=args.db)
    if not panel:
        raise SystemExit("复权日线库为空，请先运行 python main.py --adjusted-daily-sync")
    frame = collect(panel, start=args.start, step=args.step, per_day=args.per_day,
                    horizons=args.horizons, seed=args.seed)
    if frame.empty:
        raise SystemExit("没有可评估样本")
    report = analyze(frame, args.horizons, args.gate)
    report["config"] = {key: getattr(args, key) for key in ("start", "step", "per_day", "horizons", "gate", "seed")}
    report["round_trip_cost"] = ROUND_TRIP_COST
    print(format_report(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1)
    return report


if __name__ == "__main__":
    main()
