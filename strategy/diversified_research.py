"""固定方向、低频调仓的量价研究候选；只生成目标，不修改正式交易策略。"""
from collections import Counter
from dataclasses import dataclass

import numpy as np
import pandas as pd

from strategy.factor_research import validate_panel


@dataclass(frozen=True)
class DiversifiedConfig:
    mode: str = "balanced"
    rebalance_days: int = 20
    top_k: int = 20
    retention_rank: int = 30
    sector_slots: int = 4
    max_weight: float = 0.05
    trend_filter: bool = True

    def validate(self):
        if self.mode not in ("equal", "momentum", "balanced"):
            raise ValueError("未知融合方式")
        for field in ("rebalance_days", "top_k", "retention_rank", "sector_slots"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field} 必须为正整数")
        if self.retention_rank < self.top_k:
            raise ValueError("保留排名应不小于目标持仓数")
        if not np.isfinite(self.max_weight) or not 0 < self.max_weight <= 1:
            raise ValueError("单股权重无效")


def prepare_features(panel):
    """在共同日期轴计算，不用缺失日之后的观测倒填，也不把停牌期缩短。"""
    bars = validate_panel(panel)
    close = bars.pivot(index="date", columns="code", values="close")
    volume = bars.pivot(index="date", columns="code", values="volume")
    returns = close.pct_change(fill_method=None)
    # 跳过最近20日，减轻短期反转干扰；方向预先固定，不按近期IC反转。
    momentum = (close.shift(20) / close.shift(120) - 1)
    volatility = returns.rolling(60, min_periods=60).std()
    trend = (close > close.rolling(120, min_periods=120).mean()) & (momentum > 0)
    available = close.notna() & momentum.notna() & volatility.notna() & (volume > 0)
    if "eligible" in bars:
        available &= bars.pivot(index="date", columns="code", values="eligible").fillna(False).astype(bool)
    if "tradable" in bars:
        available &= bars.pivot(index="date", columns="code", values="tradable").fillna(False).astype(bool)
    return {"close": close, "momentum": momentum, "volatility": volatility,
            "trend": trend, "available": available}


def build_targets(panel, sector_map, start_date, config=None):
    """月度等权；跌出趋势或保留区即退出，排名缓冲只跟踪过去目标成员。

    sector_map 仅是调用方声明的研究分组，不能冒充历史行业中性化。
    """
    config = config or DiversifiedConfig()
    config.validate()
    features = prepare_features(panel)
    codes = features["close"].columns
    if not set(codes).issubset(sector_map) or any(not sector_map[c] for c in codes):
        raise ValueError("所有股票都需要明确的研究行业分组")
    dates = features["close"].index
    first = dates.searchsorted(pd.Timestamp(start_date))
    if first >= len(dates) - 1:
        raise ValueError("信号开始日必须早于最后执行日")
    targets, audit, previous = {}, [], set()
    for date in dates[first:-1:config.rebalance_days]:
        valid = features["available"].loc[date]
        if config.trend_filter:
            valid &= features["trend"].loc[date]
        eligible = list(valid[valid].index)
        momentum = features["momentum"].loc[date, eligible]
        volatility = features["volatility"].loc[date, eligible]
        if config.mode == "momentum":
            score = momentum.rank(pct=True)
        elif config.mode == "balanced":
            score = 0.5 * momentum.rank(pct=True) + 0.5 * (-volatility).rank(pct=True)
        else:
            score = pd.Series(0.0, index=eligible)
        ranked = sorted(eligible, key=lambda code: (-float(score[code]), code))
        rank = {code: i + 1 for i, code in enumerate(ranked)}
        retained = [code for code in ranked if code in previous and rank[code] <= config.retention_rank]
        selected, counts = [], Counter()
        for code in retained + [code for code in ranked if code not in retained]:
            if len(selected) >= config.top_k:
                break
            if counts[sector_map[code]] < config.sector_slots:
                selected.append(code)
                counts[sector_map[code]] += 1
        # 不足名额留现金，不把剩余少数股票加仓到满仓。
        weight = min(1 / config.top_k, config.max_weight)
        targets[date] = {code: weight for code in selected}
        audit.append({"date": date.strftime("%Y-%m-%d"), "eligible": len(eligible),
                      "selected": selected, "retained": [code for code in selected if code in retained],
                      "sector_counts": dict(counts), "target_exposure": weight * len(selected),
                      "weights": targets[date]})
        previous = set(selected)
    return targets, audit
