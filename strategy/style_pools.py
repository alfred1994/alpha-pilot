"""多风格选股池与横截面因子打分（基于等比复权全市场日线，收盘后计算，供次日使用）。

依据 docs/signal-drought-audit-2026-10-29.md 第 4.5 节的因子检验：
A 股 2017-2026 年低波动、避开暴涨股、中期反转的排名 IC 稳定为正，
趋势/动量为负。因此：

- ``oversold_rebound`` 超跌反弹：20 日跌幅靠前，同时排除仍在剧烈波动或近期暴涨的票；
- ``low_vol_defensive`` 低波防守：低波动、无暴涨，估值可得时要求 PE 为正且不过高；
- ``quality_value`` 基本面质量：ROE、增长、负债率、现金含量与估值（需财务快照）；
- ``trend_strength`` 强势趋势：双通道多头且动量靠前，只作影子对照，验证其是否仍为负；
- ``factor_top`` 因子分前列：按滚动 IC 加权的横截面因子分。

本模块是纯计算：不访问网络、不写数据库、不影响下单。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Optional

import numpy as np
import pandas as pd

from strategy.trend_channel import dtc_states, ema

STYLES = ("oversold_rebound", "low_vol_defensive", "quality_value", "trend_strength", "factor_top")
STYLE_LABELS = {
    "oversold_rebound": "超跌反弹",
    "low_vol_defensive": "低波防守",
    "quality_value": "基本面质量",
    "trend_strength": "强势趋势(对照)",
    "factor_top": "因子分前列",
}
# 进入因子打分的候选因子；方向不预设，由滚动 IC 决定权重正负。
SCORE_FACTORS = ("low_vol20", "anti_lottery20", "reversal20", "reversal5", "low_amount20", "momentum120", "trend_ema120")


@dataclass(frozen=True)
class UniverseRule:
    min_listed_days: int = 250
    min_amount20: float = 1e8
    min_price: float = 3.0


@dataclass(frozen=True)
class ICConfig:
    lookback: int = 250
    horizon: int = 20
    step: int = 5
    min_abs_ic: float = 0.02
    min_sign_stability: float = 0.55
    min_samples: int = 20


def compute_factors(panel: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """由宽表计算因子（行=日期，列=股票），全部只用当日及以前数据。"""
    close, high, low = panel["close"], panel["high"], panel["low"]
    daily = close.pct_change(fill_method=None)
    amount20 = panel["amount"].rolling(20, min_periods=15).mean()
    long_on, short_on = dtc_states(high, low, close)
    return {
        "ret5": close / close.shift(5) - 1,
        "ret20": close / close.shift(20) - 1,
        "ret3": close / close.shift(3) - 1,
        "vol20": daily.rolling(20, min_periods=15).std(),
        "maxret20": daily.rolling(20, min_periods=15).max(),
        "amount20": amount20,
        "momentum120": close.shift(5) / close.shift(125) - 1,
        "trend_ema120": close / ema(close, 120) - 1,
        "dtc_both": (long_on & short_on).astype(float),
        # 打分因子统一为“越大越好”的原始方向，权重符号由 IC 决定
        "low_vol20": -daily.rolling(20, min_periods=15).std(),
        "anti_lottery20": -daily.rolling(20, min_periods=15).max(),
        "reversal20": -(close / close.shift(20) - 1),
        "reversal5": -(close / close.shift(5) - 1),
        "low_amount20": -np.log(amount20.where(amount20 > 0)),
        "listed_days": close.notna().cumsum(),
        "flag20": panel["flag"].fillna(0).rolling(20, min_periods=1).max() if "flag" in panel else close * 0,
    }


def universe_mask(panel, factors, rule: UniverseRule = UniverseRule()) -> pd.DataFrame:
    """可交易股票池：上市满一年、20 日均成交额达标、价格不低于下限、近 20 日无异常日。"""
    return ((factors["listed_days"] >= rule.min_listed_days)
            & (factors["amount20"] >= rule.min_amount20)
            & (panel["raw_close"] >= rule.min_price)
            & (factors["flag20"] < 1)
            & panel["close"].notna())


def _rank_pct(series: pd.Series) -> pd.Series:
    return series.rank(pct=True)


def _pool(frame: pd.DataFrame, score: pd.Series, top_n: int, columns) -> pd.DataFrame:
    frame = frame.assign(score=score).dropna(subset=["score"]).sort_values("score", ascending=False).head(top_n)
    frame = frame.reset_index().rename(columns={"index": "code"})
    frame["rank"] = np.arange(1, len(frame) + 1)
    return frame[["code", "rank", "score", *columns]]


def _snapshot(factors, universe, date) -> pd.DataFrame:
    names = ["ret3", "ret5", "ret20", "vol20", "maxret20", "amount20", "momentum120", "trend_ema120", "dtc_both"]
    rows = pd.DataFrame({name: factors[name].loc[date] for name in names})
    return rows[universe.loc[date].reindex(rows.index).fillna(False).astype(bool)]


def select_pools(factors, universe, date, *, top_n: int = 15,
                 valuations: Optional[pd.DataFrame] = None,
                 fundamentals: Optional[pd.DataFrame] = None,
                 scores: Optional[pd.Series] = None) -> Dict[str, pd.DataFrame]:
    """为某个收盘日生成各风格候选池。valuations/fundamentals 以 code 为索引。"""
    snap = _snapshot(factors, universe, date)
    pools: Dict[str, pd.DataFrame] = {}
    if snap.empty:
        return {style: pd.DataFrame(columns=["code", "rank", "score"]) for style in STYLES}
    pe = None
    if valuations is not None and not valuations.empty and "pe_ttm" in valuations:
        pe = valuations["pe_ttm"].reindex(snap.index)

    # 超跌反弹：20日跌幅前20%，排除仍处剧烈波动(前30%)、近期暴涨(前30%)、最近3日仍在急跌的票
    drop_rank = _rank_pct(-snap["ret20"])
    calm = _rank_pct(snap["vol20"]) <= 0.7
    no_lottery = _rank_pct(snap["maxret20"]) <= 0.7
    stabilizing = snap["ret3"] >= -0.02
    ok = (drop_rank >= 0.8) & calm & no_lottery & stabilizing
    if pe is not None:
        ok &= ~(pe <= 0) | pe.isna()  # 已知亏损的不进超跌池；估值缺失不额外排除
    score = (drop_rank + _rank_pct(-snap["vol20"]) + _rank_pct(-snap["maxret20"])) / 3
    pools["oversold_rebound"] = _pool(snap[ok], score[ok], top_n, ["ret20", "vol20", "maxret20"])

    # 低波防守：波动后20%，暴涨后50%；估值可得时 0<PE<=40
    ok = (_rank_pct(snap["vol20"]) <= 0.2) & (_rank_pct(snap["maxret20"]) <= 0.5)
    if pe is not None:
        ok &= (pe > 0) & (pe <= 40) | pe.isna()
    score = (_rank_pct(-snap["vol20"]) + _rank_pct(-snap["maxret20"])) / 2
    pools["low_vol_defensive"] = _pool(snap[ok], score[ok], top_n, ["vol20", "maxret20"])

    # 基本面质量：需要财务快照
    if fundamentals is not None and not fundamentals.empty:
        f = fundamentals.reindex(snap.index)
        ok = ((f["roe"] >= 10) & (f["np_yoy"] > 0) & (f["debt_ratio"] < 60) & (f["cash_content"] >= 80))
        if pe is not None:
            ok &= (pe > 0) & (pe <= 30)
        score = (_rank_pct(f["roe"]) + _rank_pct(-(pe if pe is not None else f["roe"] * 0))
                 + _rank_pct(-snap["vol20"])) / 3
        ok = ok.fillna(False)
        pools["quality_value"] = _pool(snap[ok], score[ok], top_n, ["vol20"])
    else:
        pools["quality_value"] = pd.DataFrame(columns=["code", "rank", "score"])

    # 强势趋势（影子对照）：双通道多头且动量靠前
    ok = snap["dtc_both"] > 0
    pools["trend_strength"] = _pool(snap[ok], _rank_pct(snap["momentum120"])[ok], top_n, ["momentum120", "trend_ema120"])

    if scores is not None and not scores.empty:
        s = scores.reindex(snap.index)
        pools["factor_top"] = _pool(snap[s.notna()], s[s.notna()], top_n, ["vol20", "ret20"])
    else:
        pools["factor_top"] = pd.DataFrame(columns=["code", "rank", "score"])
    return pools


# ── 因子打分 ───────────────────────────────────────────────────────


def forward_returns(panel, horizon: int) -> pd.DataFrame:
    """t 日收盘出信号，t+1 开盘买入，t+1+horizon 开盘卖出的收益。"""
    opens = panel["open"]
    return opens.shift(-(horizon + 1)) / opens.shift(-1) - 1


def ic_weights(factors, universe, fwd: pd.DataFrame, asof_pos: int,
               config: ICConfig = ICConfig()) -> Dict[str, dict]:
    """在 asof_pos 之前、且标签已完全可见的窗口内计算各因子排名 IC 与权重。

    标签需要 horizon+1 个交易日才可见，因此最后可用的信号日为 asof_pos-horizon-1。
    权重 = IC 均值 / IC 标准差（ICIR），未通过最小 |IC| 或符号稳定性的因子权重为 0。
    """
    end = asof_pos - config.horizon - 1
    start = max(0, end - config.lookback)
    positions = list(range(start, end + 1, config.step))
    stats = {}
    if len(positions) < config.min_samples:
        return {name: {"weight": 0.0, "ic_mean": None, "samples": len(positions)} for name in SCORE_FACTORS}
    y = fwd.iloc[positions].where(universe.iloc[positions])
    y_rank = y.rank(axis=1)
    for name in SCORE_FACTORS:
        x = factors[name].iloc[positions].where(universe.iloc[positions])
        ic = x.rank(axis=1).corrwith(y_rank, axis=1).dropna()
        if len(ic) < config.min_samples or ic.std() == 0:
            stats[name] = {"weight": 0.0, "ic_mean": None, "samples": int(len(ic))}
            continue
        mean, std = float(ic.mean()), float(ic.std())
        stability = float((np.sign(ic) == np.sign(mean)).mean())
        passed = abs(mean) >= config.min_abs_ic and stability >= config.min_sign_stability
        stats[name] = {"weight": mean / std if passed else 0.0, "ic_mean": mean, "ic_ir": mean / std,
                       "sign_stability": stability, "samples": int(len(ic))}
    return stats


def factor_scores(factors, universe, date, weights: Dict[str, dict]) -> pd.Series:
    """当日横截面因子分（0-100 百分位），无有效权重时返回空序列。"""
    mask = universe.loc[date].fillna(False).astype(bool)
    total = None
    weight_sum = 0.0
    for name, stat in weights.items():
        w = float(stat.get("weight") or 0.0)
        if w == 0 or not math.isfinite(w):
            continue
        z = factors[name].loc[date][mask].rank(pct=True) - 0.5
        total = z * w if total is None else total.add(z * w, fill_value=0)
        weight_sum += abs(w)
    if total is None or weight_sum == 0:
        return pd.Series(dtype=float)
    return (total / weight_sum).rank(pct=True) * 100
