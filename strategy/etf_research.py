"""固定ETF映射与因果目标生成，不触发真实交易或按后验收益更换标的。"""
import numpy as np
import pandas as pd
import json


SECTOR_ETFS = {
    "银行": {"512800": 1.0},
    "证券保险": {"512880": 0.5, "512070": 0.5},
    "食品饮料": {"515170": 1.0},
    "家电": {"159996": 1.0},
    "医药": {"512010": 1.0},
    "电子通信": {"512760": 0.5, "515050": 0.5},
    "汽车零部件": {"516110": 1.0},
    "能源电力": {"159930": 0.5, "159611": 0.5},
    "有色化工": {"512400": 0.5, "159870": 0.5},
    "基建运输": {"516970": 0.5, "159666": 0.5},
}
BROAD_ETFS = {"510300": "沪深300", "510500": "中证500"}


def parse_identity_adjustments(text, cutoff="2026-10-09"):
    """仅接受明确的单位复权记录，空响应不是无分红证明；绝不eval远端JS。"""
    payload, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
    records = payload.get("data")
    if not isinstance(records, list) or not records or payload.get("total") != len(records):
        raise ValueError("复权事件数据为空或不完整")
    past = [r for r in records if pd.Timestamp(r["d"]) <= pd.Timestamp(cutoff)]
    if not past:
        raise ValueError("没有截至研究终点的复权记录")
    for record in past:
        if any(not np.isfinite(float(record[k])) for k in ("f","s","u")):
            raise ValueError("复权因子非有限")
        if float(record["f"]) != 1 or float(record["s"]) != 1 or float(record["u"]) != 0:
            raise ValueError("存在分红/份额调整，不能将未复权日线当作复权数据")
    return payload


def validate_etf_panel(panel):
    required = {"date", "code", "open", "high", "low", "close", "volume"}
    if panel.empty or not required.issubset(panel):
        raise ValueError("ETF面板缺必要字段")
    bars = panel.copy()
    bars["date"] = pd.to_datetime(bars["date"], errors="raise")
    allowed = set(BROAD_ETFS) | {c for m in SECTOR_ETFS.values() for c in m}
    if not set(bars.code).issubset(allowed):
        raise ValueError("只接受已固定映射的国内股票ETF")
    if bars.date.isna().any() or bars.duplicated(["date", "code"]).any():
        raise ValueError("日期缺失或重复")
    if (bars.date != bars.date.dt.normalize()).any():
        raise ValueError("只接受日线")
    for _, group in bars.groupby("code"):
        if not group.date.is_monotonic_increasing:
            raise ValueError("日线乱序")
    cols = ["open", "high", "low", "close", "volume"]
    bars[cols] = bars[cols].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(bars[cols]).all().all() or (bars[cols] <= 0).any().any():
        raise ValueError("ETF量价必须有限且为正")
    if (bars.high < bars[["open", "close", "low"]].max(axis=1)).any() or (bars.low > bars[["open", "close"]].min(axis=1)).any():
        raise ValueError("OHLC异常")
    return bars.sort_values(["date", "code"]).reset_index(drop=True)


def sector_weights_to_etfs(sector_weights):
    targets = {}
    if not set(sector_weights).issubset(SECTOR_ETFS):
        raise ValueError("未知行业")
    if any(not np.isfinite(w) or w < 0 for w in sector_weights.values()) or sum(sector_weights.values()) > 1 + 1e-10:
        raise ValueError("行业权重不合法")
    for sector, weight in sector_weights.items():
        for code, proportion in SECTOR_ETFS[sector].items():
            if weight > 0:
                targets[code] = targets.get(code, 0) + float(weight * proportion)
    return targets


def stock_targets_to_etfs(selections, stock_sectors):
    result = {}
    for row in selections:
        weights = {}
        for code, weight in row["weights"].items():
            sector = stock_sectors[code]
            weights[sector] = weights.get(sector, 0) + weight
        result[pd.Timestamp(row["date"])] = sector_weights_to_etfs(weights)
    return result


def build_etf_targets(panel, mode, start="2025-05-29", rebalance_days=20, include_latest=False):
    """三种预设：行业等权、行业趋势过滤、最多5行业动量轮动。"""
    if mode not in ("sector_equal", "sector_trend", "sector_rotation"):
        raise ValueError("未知ETF策略")
    if not isinstance(rebalance_days,int) or isinstance(rebalance_days,bool) or rebalance_days < 1:
        raise ValueError("调仓间隔必须为正整数")
    bars = validate_etf_panel(panel)
    close = bars.pivot(index="date", columns="code", values="close")
    expected = {c for m in SECTOR_ETFS.values() for c in m}
    if not expected.issubset(close):
        raise ValueError("ETF行业覆盖不足，不能丢弃行业后重新等权")
    # 固定初始金额的行业ETF篮子，只作信号指数；真实成交由共享账本处理。
    sectors = pd.DataFrame(index=close.index)
    for name, members in SECTOR_ETFS.items():
        series = close[list(members)]
        if series.iloc[0].isna().any():
            raise ValueError("起始日行业ETF数据不完整")
        sectors[name] = (series / series.iloc[0] * pd.Series(members)).sum(axis=1, min_count=len(members))
    momentum = sectors.shift(20) / sectors.shift(120) - 1
    trend = (sectors > sectors.rolling(120, min_periods=120).mean()) & (momentum > 0)
    # 前向观测可记录最新已收盘日的信号，但执行器必须另行保证在未来开盘执行。
    stop = None if include_latest else -1
    signal_dates = close.index[close.index.searchsorted(pd.Timestamp(start)):stop:rebalance_days]
    if not len(signal_dates):
        raise ValueError("无信号日期")
    targets, audit = {}, []
    for date in signal_dates:
        if sectors.loc[date].isna().any() or momentum.loc[date].isna().any():
            raise ValueError("信号日缺历史行情，不提前假设能成交")
        selected = list(SECTOR_ETFS) if mode == "sector_equal" else list(trend.columns[trend.loc[date]])
        if mode == "sector_rotation":
            selected = sorted(selected, key=lambda sector: (-float(momentum.loc[date,sector]), sector))[:5]
        weight = 0.2 if mode == "sector_rotation" else 0.1
        targets[date] = sector_weights_to_etfs({name:weight for name in selected})
        audit.append({"date":date.strftime("%Y-%m-%d"), "sectors":selected,
                      "weights":targets[date], "exposure":sum(targets[date].values())})
    return targets, audit
