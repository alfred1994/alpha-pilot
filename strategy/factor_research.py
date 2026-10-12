"""时间隔离的多因子研究闭环：训练期检验/去冗余/融合，样本外组合评估。"""
from dataclasses import asdict, dataclass, replace
import hashlib
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from portfolio.factor_backtest import ExecutionConfig, run_portfolio, summarize_curve
from strategy.ml_baseline import BAR_COLS, FEATURE_COLS, build_baseline_features


@dataclass(frozen=True)
class FactorConfig:
    train_days: int = 250
    valid_days: int = 60
    rebalance_days: int = 5
    min_stocks: int = 5
    top_k: int = 5
    max_factors: int = 5
    min_ic_days: int = 30
    min_abs_rank_ic: float = 0.02
    min_sign_stability: float = 0.55
    max_correlation: float = 0.85
    fusion: str = "ic_weighted"
    neutralize: bool = False

    def validate(self):
        for key in ("train_days", "valid_days", "rebalance_days", "min_stocks", "top_k", "max_factors", "min_ic_days"):
            value = getattr(self, key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{key} 必须是正整数")
        if self.min_stocks < 5 or self.train_days < self.min_ic_days + self.rebalance_days + 1:
            raise ValueError("横截面至少 5 只股票，训练窗需覆盖检验日数及标签隔离期")
        if self.top_k > self.min_stocks:
            raise ValueError("top_k 不能超过 min_stocks")
        for key in ("min_abs_rank_ic", "min_sign_stability", "max_correlation"):
            value = getattr(self, key)
            if not np.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{key} 必须在 [0,1] 内")
        if self.fusion not in ("ic_weighted", "equal"):
            raise ValueError("fusion 必须是 ic_weighted 或 equal")


def load_local_panel(codes, start_date, end_date, db_path=None):
    """仅查询已存在的 SQLite 表；保留可选的历史交易元数据。"""
    codes = list(dict.fromkeys(codes or []))
    if not codes:
        raise ValueError("请显式指定股票池 --stocks；不能用单股代替横截面研究")
    if pd.Timestamp(start_date) > pd.Timestamp(end_date):
        raise ValueError("开始日期不能晚于结束日期")
    if db_path is None:
        from config import DATA_DIR
        db_path = Path(DATA_DIR) / "quant.db"
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(k_daily)")}
        wanted = ["code"] + BAR_COLS + ["source", "eligible", "tradable", "raw_open", "raw_close",
                                      "limit_up", "limit_down", "industry", "market_cap", "corporate_action"]
        selected = [c for c in wanted if c in columns]
        if not set(["code"] + BAR_COLS).issubset(selected):
            raise ValueError("本地 k_daily 缺少必要日线字段")
        return pd.read_sql_query(
            f"SELECT {','.join(selected)} FROM k_daily WHERE code IN ({','.join('?' for _ in codes)}) "
            "AND date >= ? AND date <= ? ORDER BY date, code",
            connection, params=codes + [start_date, end_date],
        )
    finally:
        connection.close()


def validate_panel(panel):
    required = ["code"] + BAR_COLS
    if panel.empty or not set(required).issubset(panel.columns):
        raise ValueError("面板需要非空 code/date/open/high/low/close/volume")
    frame = panel.copy().reset_index(drop=True)
    frame["code"] = frame["code"].astype(str)
    # 本版本仅做普通主板，避免把创业板/科创板上市初期和整手规则套成 10%。
    supported = frame["code"].str.fullmatch(r"(?:000|001|002|003|600|601|603|605)\d{3}")
    if not supported.all() or (frame["code"] == "000300").any():
        raise ValueError("本版仅支持 6 位普通主板股票代码，不支持指数/科创板/创业板")
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    if frame["date"].isna().any() or (frame["date"] != frame["date"].dt.normalize()).any():
        raise ValueError("面板日期必须是完整日线日期")
    if frame.duplicated(["date", "code"]).any():
        raise ValueError("面板含重复 date/code")
    for _, group in frame.groupby("code"):
        if not group["date"].is_monotonic_increasing:
            raise ValueError("每只股票的日线必须按时间递增")
    for col in BAR_COLS[1:]:
        frame[col] = pd.to_numeric(frame[col], errors="raise")
    values = frame[BAR_COLS[1:]].to_numpy(dtype=float)
    if (not np.isfinite(values).all() or (frame[["open", "high", "low", "close"]] <= 0).any().any()
            or (frame["volume"] < 0).any()):
        raise ValueError("OHLC 必须有限且为正，成交量必须有限且非负")
    if ((frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame["low"] > frame[["open", "close"]].min(axis=1)).any()):
        raise ValueError("OHLC 高低价关系异常")
    for column in ("eligible", "tradable", "corporate_action"):
        if column in frame and (frame[column].isna().any() or not frame[column].isin([True, False, 0, 1]).all()):
            raise ValueError(f"{column} 必须是完整的布尔值/0/1，不接受字符串")
    if "corporate_action" in frame and frame["corporate_action"].any():
        raise ValueError("本版未实现分红送转账务，请排除公司行动区间或先提供相应账务支持")
    for col in ("raw_open", "raw_close", "limit_up", "limit_down", "market_cap"):
        if col in frame:
            frame[col] = pd.to_numeric(frame[col], errors="raise")
            supplied = frame[col].dropna()
            if not np.isfinite(supplied).all() or (supplied <= 0).any():
                raise ValueError(f"{col} 如提供则必须有限且为正")
    for _, group in frame.groupby("code"):
        raw_open = group.get("raw_open", pd.Series(np.nan, index=group.index))
        raw_close = group.get("raw_close", pd.Series(np.nan, index=group.index))
        if (raw_open.notna().any() or raw_close.notna().any()) and (raw_open.isna().any() or raw_close.isna().any()):
            raise ValueError("同一股票的 raw_open/raw_close 必须配对且全时段完整，禁止混用复权价格")
    return frame.sort_values(["date", "code"]).reset_index(drop=True)


def build_factor_panel(panel, horizon=5, neutralize=False):
    """分别计算时序特征，再按当天横截面去极值、可选中性化和秩标准化。"""
    pieces = []
    dates = sorted(panel["date"].unique())
    date_position = {date: index for index, date in enumerate(dates)}
    for code, group in panel.groupby("code", sort=True):
        group = group.sort_values("date").copy()
        # 零量/停牌行不构成新的观测；不把未来恢复交易日搬到缺失日期。
        available = group["volume"] > 0
        if "tradable" in group:
            available &= group["tradable"].astype(bool)
        tradable = group.loc[available].reset_index(drop=True)
        if tradable.empty:
            continue
        feat = build_baseline_features(tradable)
        part = tradable[["date", "code"]].copy()
        part[FEATURE_COLS] = feat
        for col in ("eligible", "industry", "market_cap"):
            if col in tradable:
                part[col] = tradable[col]
        by_date = group.set_index("date")
        returns, ends = [], []
        for date in part["date"]:
            pos = date_position[date]
            if pos + horizon + 1 >= len(dates):
                returns.append(np.nan)
                ends.append(pd.NaT)
                continue
            entry_date, end_date = dates[pos + 1], dates[pos + horizon + 1]
            ends.append(end_date)
            if entry_date not in by_date.index or end_date not in by_date.index:
                returns.append(np.nan)
                continue
            entry, end = by_date.loc[entry_date], by_date.loc[end_date]
            if (entry["volume"] <= 0 or end["volume"] <= 0
                    or not bool(entry.get("tradable", True)) or not bool(end.get("tradable", True))):
                returns.append(np.nan)
            else:
                returns.append(float(end["open"] / entry["open"] - 1))
        part["forward_return"], part["label_end"] = returns, pd.to_datetime(ends)
        pieces.append(part)
    if not pieces:
        raise ValueError("没有可计算因子的交易日线")
    factors = pd.concat(pieces, ignore_index=True)
    factors = factors[np.isfinite(factors[FEATURE_COLS]).all(axis=1)].copy()
    if "eligible" in factors:
        factors = factors[factors["eligible"].astype(bool)].copy()
    if neutralize and not {"industry", "market_cap"}.issubset(factors.columns):
        raise ValueError("行业/市值中性化需要逐日 industry 和 market_cap，不能使用今天的分类回填历史")
    for _, cross in factors.groupby("date"):
        raw = cross[FEATURE_COLS]
        clipped = raw.clip(lower=raw.quantile(0.01), upper=raw.quantile(0.99), axis=1)
        if neutralize:
            if cross["industry"].isna().any() or cross["market_cap"].isna().any():
                raise ValueError("中性化元数据缺失")
            industry = pd.get_dummies(cross["industry"].astype(str), drop_first=True, dtype=float)
            design = np.column_stack([np.ones(len(cross)), np.log(cross["market_cap"]), industry])
            if len(cross) <= design.shape[1] + 2:
                factors.loc[cross.index, FEATURE_COLS] = np.nan
                continue
            clipped = pd.DataFrame(clipped.to_numpy() - design @ np.linalg.lstsq(design, clipped.to_numpy(), rcond=None)[0],
                                   index=cross.index, columns=FEATURE_COLS)
        ranked = clipped.rank(method="average", pct=True)
        factors.loc[cross.index, FEATURE_COLS] = ranked - ranked.mean()
    return factors.sort_values(["date", "code"]).reset_index(drop=True)


def factor_diagnostics(frame, columns, min_stocks=5):
    """日横截面 Pearson IC、RankIC 和五分组收益；只评估具有完整标签的行。"""
    records = {col: [] for col in columns}
    for date, group in frame.groupby("date"):
        group = group.dropna(subset=["forward_return"] + list(columns))
        if len(group) < min_stocks or group["forward_return"].nunique() < 2:
            continue
        y = group["forward_return"]
        yrank = y.rank()
        for col in columns:
            x = group[col]
            if x.nunique() < 2:
                continue
            ordered = group.sort_values([col, "code"])
            buckets = np.array_split(ordered["forward_return"].to_numpy(), 5)
            means = [float(bucket.mean()) for bucket in buckets]
            records[col].append({"date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                                 "ic": float(x.corr(y)), "rank_ic": float(x.rank().corr(yrank)),
                                 "groups": means, "top_minus_bottom": means[-1] - means[0]})
    result = {}
    for col, daily in records.items():
        if not daily:
            result[col] = {"ic_days": 0, "ic_mean": None, "rank_ic_mean": None,
                           "rank_ic_ir": None, "sign_stability": None, "half_sign_consistent": False,
                           "group_mean_returns": None, "top_minus_bottom": None}
            continue
        rank_ic = np.array([item["rank_ic"] for item in daily])
        mean = float(rank_ic.mean())
        direction = 1 if mean > 0 else -1
        std = float(rank_ic.std(ddof=1)) if len(rank_ic) > 1 else 0.0
        halves = np.array_split(rank_ic, 2)
        result[col] = {
            "ic_days": len(daily), "ic_mean": float(np.mean([item["ic"] for item in daily])),
            "rank_ic_mean": mean, "rank_ic_ir": mean / std if std > 1e-12 else None,
            "sign_stability": float((rank_ic * direction > 0).mean()),
            "half_sign_consistent": bool(all(len(half) and half.mean() * direction > 0 for half in halves)),
            "group_mean_returns": np.mean([item["groups"] for item in daily], axis=0).tolist(),
            "top_minus_bottom": float(np.mean([item["top_minus_bottom"] for item in daily])),
        }
    return result


def fit_fusion(train, config):
    """只在训练段确定因子方向、相关性筛选及权重；未过门槛则空仓。"""
    diagnostics = factor_diagnostics(train, FEATURE_COLS, config.min_stocks)
    candidates = []
    for col, metric in diagnostics.items():
        reasons = []
        if metric["ic_days"] < config.min_ic_days:
            reasons.append("insufficient_ic_days")
        mean = metric["rank_ic_mean"] or 0.0
        if abs(mean) < config.min_abs_rank_ic or abs(mean) < 1e-12:
            reasons.append("weak_rank_ic")
        if (metric["sign_stability"] or 0) < config.min_sign_stability or not metric["half_sign_consistent"]:
            reasons.append("unstable_sign")
        metric["exclusions"] = reasons
        if not reasons:
            candidates.append(col)
    candidates.sort(key=lambda col: (-abs(diagnostics[col]["rank_ic_mean"]), col))
    correlations = train[FEATURE_COLS].corr()
    selected = []
    for col in candidates:
        redundant = [other for other in selected if abs(correlations.loc[col, other]) >= config.max_correlation]
        if redundant:
            diagnostics[col]["exclusions"].append("correlated_with:" + redundant[0])
        elif len(selected) >= config.max_factors:
            diagnostics[col]["exclusions"].append("factor_count_limit")
        else:
            selected.append(col)
    raw = {col: (np.sign(diagnostics[col]["rank_ic_mean"]) if config.fusion == "equal"
                 else diagnostics[col]["rank_ic_mean"]) for col in selected}
    total = sum(abs(value) for value in raw.values())
    weights = {col: float(value / total) for col, value in raw.items()}
    return {"weights": weights, "diagnostics": diagnostics,
            "selected_correlations": {a: {b: float(correlations.loc[a, b]) for b in selected} for a in selected}}


def run_factor_research(panel, config=None, execution=None):
    config = config or FactorConfig()
    execution = execution or ExecutionConfig()
    config.validate()
    execution.validate()
    if config.top_k > execution.max_positions:
        raise ValueError("top_k 不能超过组合最大持仓数")
    bars = validate_panel(panel)
    coverage = {str(code): {"rows": len(group), "start": group["date"].min().strftime("%Y-%m-%d"),
                            "end": group["date"].max().strftime("%Y-%m-%d")}
                for code, group in bars.groupby("code")}
    if len(coverage) < config.min_stocks:
        raise ValueError(f"横截面至少需 {config.min_stocks} 只股票，实际 {len(coverage)}；不能用单股回测代替")
    factors = build_factor_panel(bars, config.rebalance_days, config.neutralize)
    dates = sorted(bars["date"].unique())
    # 起点只依赖过去/当日的特征覆盖，不因未来标签缺失删掉验证股票或日期。
    counts = factors.dropna(subset=FEATURE_COLS).groupby("date").size()
    starts = counts[counts >= config.min_stocks]
    if starts.empty:
        raise ValueError("预热后没有至少 5 只股票同时具备完整因子的横截面")
    first = dates.index(starts.index[0])
    sample_dates = dates[first:]
    if len(sample_dates) < config.train_days + config.valid_days + 1:
        raise ValueError(f"日线跨度不足：预热后 {len(sample_dates)} 日，需训练 {config.train_days} + 验证 {config.valid_days} + 执行 1 日")
    signal_dates = sample_dates[:-1]
    folds, scores = [], []
    for start in range(config.train_days, len(signal_dates), config.valid_days):
        valid_dates = signal_dates[start:start + config.valid_days]
        train_dates = signal_dates[start - config.train_days:start]
        train_candidates = factors[factors["date"].isin(train_dates)]
        train = train_candidates[(train_candidates["label_end"] < valid_dates[0])
                                 & train_candidates["forward_return"].notna()].dropna(subset=FEATURE_COLS)
        fusion = fit_fusion(train, config)
        valid = factors[factors["date"].isin(valid_dates)].dropna(subset=FEATURE_COLS).copy()
        weights = fusion["weights"]
        valid["score"] = sum(valid[col] * weight for col, weight in weights.items()) if weights else np.nan
        valid["fold"] = len(folds) + 1
        scores.append(valid)
        folds.append({
            "fold": len(folds) + 1, "train_start": pd.Timestamp(train_dates[0]).strftime("%Y-%m-%d"),
            "train_end": train["date"].max().strftime("%Y-%m-%d") if len(train) else None,
            "train_label_end": train["label_end"].max().strftime("%Y-%m-%d") if len(train) else None,
            "validation_start": pd.Timestamp(valid_dates[0]).strftime("%Y-%m-%d"),
            "validation_end": pd.Timestamp(valid_dates[-1]).strftime("%Y-%m-%d"),
            "train_rows": len(train), "purged_or_unlabelled_rows": len(train_candidates) - len(train),
            "fusion": fusion, "oos_factor_diagnostics": factor_diagnostics(valid, FEATURE_COLS, config.min_stocks),
            "oos_fused_diagnostics": factor_diagnostics(valid, ["score"], config.min_stocks)["score"],
        })
    scored = pd.concat(scores, ignore_index=True)
    targets, benchmark_targets, selections = {}, {}, []
    rebalance_dates = signal_dates[config.train_days::config.rebalance_days]
    for date in rebalance_dates:
        cross = scored[scored["date"] == date]
        ranked = cross.dropna(subset=["score"]).sort_values(["score", "code"], ascending=[False, True])
        selected = ranked.head(config.top_k) if len(ranked) >= config.min_stocks else ranked.iloc[:0]
        weight = min(1 / config.top_k, execution.max_weight)
        targets[date] = {str(code): weight for code in selected["code"]}
        # 等权对照不读取未来标签，使用相同特征覆盖/成员资格和调仓日期。
        eligible = cross["code"].tolist() if len(cross) >= config.min_stocks else []
        benchmark_targets[date] = {str(code): min(1 / len(eligible), execution.max_weight) for code in eligible}
        selections.append({"date": pd.Timestamp(date).strftime("%Y-%m-%d"), "available_stocks": len(cross),
                           "weights": targets[date], "ranking": ranked[["code", "score"]].to_dict("records"),
                           "reason": "ranked_top_k" if len(selected) else "insufficient_coverage_or_no_valid_factor"})
    strategy = run_portfolio(bars, targets, execution)
    benchmark = run_portfolio(bars, benchmark_targets, replace(execution, max_positions=max(len(coverage), execution.max_positions)))
    for fold in folds:
        execution_start = pd.Timestamp(dates[dates.index(pd.Timestamp(fold["validation_start"])) + 1]).strftime("%Y-%m-%d")
        execution_end = pd.Timestamp(dates[dates.index(pd.Timestamp(fold["validation_end"])) + 1]).strftime("%Y-%m-%d")
        fold["execution_start"], fold["execution_end"] = execution_start, execution_end
        for name, book in (("strategy_net", strategy), ("equal_weight_net", benchmark)):
            previous = [row["equity"] for row in book["curve"] if row["date"] < execution_start]
            start_equity = previous[-1] if previous else execution.capital
            observations = [row for row in book["curve"] if execution_start <= row["date"] <= execution_end]
            trades = [trade for trade in book["trades"] if execution_start <= trade["date"] <= execution_end]
            # 从连续净值曲线切片，不在折边界重置现金、仓位或重复收费。
            fold[name] = summarize_curve(observations, trades, start_equity)
        fold["excess_total_return"] = fold["strategy_net"]["total_return"] - fold["equal_weight_net"]["total_return"]
    missing = [field for field in ("raw_open", "raw_close", "tradable", "limit_up", "limit_down", "eligible")
               if field not in bars or bars[field].isna().any()]
    warnings = [
        "仅研究；因子门槛是筛选规则，不是统计显著性证明，不自动晋级或改正式模拟账户。",
        "输入股票池与 eligible 的历史时点真实性须由数据提供者核验；不自动消除幸存者偏差。",
        "固定费率，未重建历史税率；未模拟开盘集合竞价排队、冲击、分红送转、退市现金回收。",
        "缺行情的存量仓位使用最后收盘估值并记录 stale_marks；期末不虚构平仓。",
        "多日收益标签相互重叠，ICIR 未年化，不可用作独立样本显著性检验。",
        "重复查看 OOS 再调参会污染验证；需新的最终留出期及后续模拟盘验证。",
    ]
    if execution.mode == "research":
        warnings.append("近似成交：缺少 raw 价格时使用复权价；缺少涨跌停价时假设普通主板上下 10%，不适用 ST/上市初期/除权日。")
    if not config.neutralize:
        warnings.append("未做行业/市值中性化；可提供逐日历史元数据后开启 neutralize。")
    if missing:
        warnings.append("缺少完整元数据：" + ", ".join(missing))
    input_hash = hashlib.sha256(bars.to_json(orient="records", date_format="iso").encode()).hexdigest()
    execution_status = "completed" if strategy["trades"] else "no_trades"
    if execution.mode == "strict" and any(field in missing for field in ("raw_open", "raw_close", "tradable", "limit_up", "limit_down")):
        execution_status = "incomplete_execution_metadata"
    elif any(row["stale_marks"] for book in (strategy, benchmark) for row in book["curve"]):
        execution_status = "incomplete_valuation"
    from research.audit import audit_panel
    data_audit = audit_panel(bars, panel.attrs.get("calendar"), panel.attrs.get("metadata"))
    return {
        "status": "research_only", "execution_status": execution_status, "data_audit": data_audit,
        "version": "factor-loop-v1", "config": asdict(config),
        "input_sha256": input_hash, "library_versions": {"numpy": np.__version__, "pandas": pd.__version__},
        "sources": {str(key): int(count) for key, count in bars["source"].fillna("unknown").value_counts().items()} if "source" in bars else {"unknown": len(bars)},
        "coverage": coverage, "missing_metadata": missing, "factors": FEATURE_COLS,
        "folds": folds, "selections": selections, "strategy": strategy, "equal_weight_benchmark": benchmark,
        "cash_return": 0.0,
        "excess_total_return": strategy["metrics"]["total_return"] - benchmark["metrics"]["total_return"],
        "validation_summary": {"folds": len(folds),
                               "folds_with_selected_factors": sum(bool(fold["fusion"]["weights"]) for fold in folds),
                               "folds_with_positive_excess": sum(fold["excess_total_return"] > 0 for fold in folds)},
        "promotion_allowed": False, "warnings": warnings,
    }
