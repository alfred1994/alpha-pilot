"""离线横截面 LightGBM 研究：因果因子、可成交时序标签和连续组合账本。"""
from dataclasses import asdict, dataclass, replace
import time

import numpy as np
import pandas as pd

from portfolio.factor_backtest import ExecutionConfig, run_portfolio, summarize_curve
from research.audit import audit_panel, records
from strategy.etf_research import BROAD_ETFS, validate_etf_panel
from strategy.factor_research import factor_diagnostics, validate_panel


WINDOWS = (5, 10, 20, 60)
FACTOR_NAMES = (
    "return", "close_ma", "volatility", "volume_ratio", "volume_cv", "range",
    "position", "drawdown", "intraday_mean", "intraday_std", "gap_mean",
    "gap_std", "up_fraction", "return_volume_corr",
)
FEATURE_COLS = [f"{name}_{window}" for window in WINDOWS for name in FACTOR_NAMES]
FEATURE_COLS += ["body", "daily_range", "upper_shadow", "lower_shadow"]
FEATURE_VERSION = "dimensionless-60-causal-v1"
MODEL_PARAMS = dict(
    objective="regression", n_estimators=160, learning_rate=0.04,
    max_depth=4, num_leaves=15, min_child_samples=50,
    colsample_bytree=0.85, reg_alpha=0.1, reg_lambda=1.0,
    random_state=42, deterministic=True, force_col_wise=True, verbosity=-1,
)
LIMITATIONS = [
    "历史回放仅用于研究；不保存或晋级交易模型，不修改账户和正式交易信号。",
    "当前股票池回溯历史存在选择/幸存者偏差；字段声明不证明历史真实性。",
    "收益标签是次日开盘至第 N+1 日开盘的比例成本估计，不含最低佣金、整手及受阻成交；实际组合另用账本计费。",
    "标签按当日可标注股票的平均收益中心化；预测的是相对强弱，不是账户收益率。",
    "多日标签及同日股票相关，样本行数不等于独立样本数，ICIR 不作为显著性检验。",
    "看过的历史不是新的独立测试；必须冻结规则后用新数据做前向模拟验证。",
    "特征重要性反映树分裂贡献，不表示因果；没有自动挑选最优回测的规则。",
]


@dataclass(frozen=True)
class MLResearchConfig:
    train_days: int = 250
    valid_days: int = 60
    horizon: int = 5
    top_k: int = 20
    min_stocks: int = 20
    threads: int = 3

    def validate(self):
        for key, value in asdict(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{key} 必须为正整数")
        if self.min_stocks < 5 or self.top_k > self.min_stocks:
            raise ValueError("至少 5 只股票，top_k 不得超过 min_stocks")
        if self.train_days < 30 or self.horizon + 1 >= self.train_days:
            raise ValueError("训练窗至少 30 日且必须覆盖标签隔离期")
        if self.threads > 3:
            raise ValueError("第一阶段研究最多使用 3 个计算线程")


def build_features(bars):
    """按给定交易日期轴计算 60 个无量纲候选；缺失观测不向前填充。"""
    close, opening = bars["close"], bars["open"]
    high, low, volume = bars["high"], bars["low"], bars["volume"]
    returns = close.pct_change(fill_method=None)
    volume_change = volume / volume.shift(1).replace(0, np.nan) - 1
    intraday, gap = close / opening - 1, opening / close.shift(1) - 1
    values = {}
    for window in WINDOWS:
        hi, lo = high.rolling(window).max(), low.rolling(window).min()
        vol_mean = volume.rolling(window).mean().replace(0, np.nan)
        correlation = returns.rolling(window).corr(volume_change)
        complete = (returns.rolling(window).count() == window) & (volume_change.rolling(window).count() == window)
        constant = (returns.rolling(window).std() <= 1e-12) | (volume_change.rolling(window).std() <= 1e-12)
        correlation = correlation.mask(complete & constant, 0.0).clip(-1, 1)
        features = (
            close / close.shift(window) - 1,
            close / close.rolling(window).mean() - 1,
            returns.rolling(window).std(), volume / vol_mean,
            volume.rolling(window).std() / vol_mean, (hi - lo) / close,
            ((close - lo) / (hi - lo).replace(0, np.nan)).mask(hi == lo, 0.5),
            close / hi - 1, intraday.rolling(window).mean(), intraday.rolling(window).std(),
            gap.rolling(window).mean(), gap.rolling(window).std(),
            (returns > 0).astype(float).where(returns.notna()).rolling(window).mean(), correlation,
        )
        values.update({f"{name}_{window}": value for name, value in zip(FACTOR_NAMES, features)})
    values.update(body=intraday, daily_range=(high - low) / close,
                  upper_shadow=(high - pd.concat([opening, close], axis=1).max(axis=1)) / close,
                  lower_shadow=(pd.concat([opening, close], axis=1).min(axis=1) - low) / close)
    return pd.DataFrame(values, index=bars.index)[FEATURE_COLS].replace([np.inf, -np.inf], np.nan)


def build_samples(bars, sessions, horizon, execution):
    """预测候选只按当日信息过滤；未来缺失只影响标签，不能影响当天入选资格。"""
    dates = pd.DatetimeIndex(sessions)
    pieces = []
    cost_multiplier = ((1 - execution.slippage) * (1 - execution.commission - execution.stamp_tax)
                       / ((1 + execution.slippage) * (1 + execution.commission)))
    for code, group in bars.groupby("code", sort=True):
        daily = group.set_index("date").reindex(dates)
        present = daily["close"].notna()
        available = present & (daily["volume"] > 0)
        if "tradable" in daily:
            available &= daily["tradable"].eq(True)
        observed = daily.copy()
        observed.loc[~available, ["open", "high", "low", "close", "volume"]] = np.nan
        features = build_features(observed)
        part = features.copy()
        part["date"], part["code"] = dates, code
        entry = daily["open"].shift(-1)
        exit_price = daily["open"].shift(-horizon - 1)
        has_label = available.shift(-1, fill_value=False) & available.shift(-horizon - 1, fill_value=False)
        part["net_forward_return"] = (exit_price / entry * cost_multiplier - 1).where(has_label)
        part["label_end"] = pd.Series(dates, index=dates).shift(-horizon - 1)
        usable = available & np.isfinite(features).all(axis=1)
        if "eligible" in daily:
            usable &= daily["eligible"].eq(True)
        pieces.append(part.loc[usable])
    samples = pd.concat(pieces, ignore_index=True)
    if samples.empty:
        raise ValueError("60 日指标预热后无有效样本；不填充缺失历史")
    # 同日排名只使用当日可见特征，不读取未来标签是否存在。
    samples[FEATURE_COLS] = (samples.groupby("date")[FEATURE_COLS].rank(pct=True) - 0.5).astype("float32")
    samples["target"] = samples["net_forward_return"] - samples.groupby("date")["net_forward_return"].transform("mean")
    return samples.sort_values(["date", "code"]).reset_index(drop=True)


def _make_model(config, model_factory):
    if model_factory is not None:
        return model_factory()
    try:
        from lightgbm import LGBMRegressor
    except ImportError as exc:
        raise RuntimeError("需要当前解释器安装 requirements.txt 中的 lightgbm；不会静默替换模型") from exc
    return LGBMRegressor(**MODEL_PARAMS, n_jobs=config.threads)


def _prediction_metrics(frame, min_stocks):
    valid = frame.dropna(subset=["target", "net_forward_return", "score"])
    diagnostics = factor_diagnostics(valid.rename(columns={"target": "forward_return"}), ["score"], min_stocks)["score"]
    return {
        "labelled_rows": len(valid), "dates": int(valid["date"].nunique()),
        "rmse_excess_return": float(np.sqrt(np.mean((valid["score"] - valid["target"]) ** 2))) if len(valid) else None,
        "zero_prediction_rmse": float(np.sqrt(np.mean(valid["target"] ** 2))) if len(valid) else None,
        "cross_sectional": diagnostics,
    }


def _targets(predictions, signal_dates, config, execution, column):
    by_date = {date: cross for date, cross in predictions.groupby("date")}
    targets, selections = {}, []
    for date in signal_dates:
        cross = by_date.get(date)
        weights = {}
        chosen = []
        if cross is not None and len(cross) >= config.min_stocks:
            chosen = cross.sort_values([column, "code"], ascending=[False, True]).head(config.top_k)["code"].tolist()
            weights = {code: min(1 / config.top_k, execution.max_weight) for code in chosen}
        targets[date] = weights
        selections.append({"date": date.strftime("%Y-%m-%d"), "weights": weights,
                           "available": 0 if cross is None else len(cross),
                           "reason": "ranked" if chosen else "insufficient_current_coverage"})
    return targets, selections


def _slice_metrics(book, start, end, capital):
    before = [day["equity"] for day in book["curve"] if day["date"] < start]
    curve = [day for day in book["curve"] if start <= day["date"] <= end]
    trades = [trade for trade in book["trades"] if start <= trade["date"] <= end]
    return summarize_curve(curve, trades, before[-1] if before else capital)


def _book_summary(book):
    return dict(book["metrics"], blocked_counts=book["blocked_counts"],
                stale_mark_days=sum(bool(day["stale_marks"]) for day in book["curve"]),
                average_exposure=float(np.mean([1 - day["cash"] / day["equity"] for day in book["curve"]])))


def run_ml_research(panel, benchmark_panel, config=None, execution=None, *,
                    calendar=None, metadata=None, benchmark_metadata=None,
                    expected_codes=None, model_factory=None, progress=None):
    """只计算，不调用数据供应商/LLM/Broker，不写生产库或模型。"""
    started = time.monotonic()
    config = config or MLResearchConfig()
    execution = execution or ExecutionConfig(max_positions=20, max_weight=0.05)
    config.validate()
    execution.validate()
    if config.top_k > execution.max_positions:
        raise ValueError("top_k 超过组合最大持仓数")
    emit = progress or (lambda event: None)
    emit({"stage": "audit"})
    audit = audit_panel(panel, calendar, metadata, expected_codes)
    benchmarks = pd.DataFrame(benchmark_panel).copy()
    if "code" in benchmarks:
        benchmarks = benchmarks[benchmarks["code"].isin(BROAD_ETFS)].copy()
    benchmark_audit = audit_panel(benchmarks, calendar, benchmark_metadata, list(BROAD_ETFS))
    base = {
        "status": "waiting_data", "research_only": True, "promotion_allowed": False,
        "interval_role": "review", "config": asdict(config), "execution": asdict(execution),
        "feature_version": FEATURE_VERSION, "features": FEATURE_COLS,
        "model_params": dict(MODEL_PARAMS, n_jobs=config.threads),
        "input_sha256": audit["input_sha256"], "data_audit": audit,
        "benchmark_audit": benchmark_audit, "limitations": LIMITATIONS,
    }
    audits = (audit, benchmark_audit)
    if any(a["status"] == "unavailable" for a in audits):
        return dict(base, reasons=sorted({reason for a in audits for reason in a["errors"]}))
    if execution.mode == "strict" and any(a["status"] != "strict_ready" for a in audits):
        return dict(base, reasons=["strict_data_not_ready"])
    bars = validate_panel(pd.DataFrame(panel))
    benchmarks = validate_etf_panel(benchmarks)
    # 行情日期并集仅作为观测轴，不冒充独立交易日历；审计缺口仍保留。
    sessions = sorted(set(bars["date"]) | set(benchmarks["date"]))
    if calendar is not None:
        sessions = sorted(pd.Timestamp(day) for day in calendar
                          if min(sessions) <= pd.Timestamp(day) <= max(sessions))
    emit({"stage": "features", "rows": len(bars), "symbols": int(bars["code"].nunique())})
    samples = build_samples(bars, sessions, config.horizon, execution)
    coverage = samples.groupby("date").size()
    available = coverage[coverage >= config.min_stocks]
    if available.empty:
        raise ValueError("没有满足最小股票数的有效横截面")
    first = sessions.index(available.index[0])
    dates = sessions[first:]
    if len(dates) < config.train_days + config.valid_days + 1:
        raise ValueError("预热后日期不足以覆盖完整训练、验证及下一开盘")
    evaluation_dates = dates[config.train_days:]
    for code in BROAD_ETFS:
        if set(evaluation_dates) - set(benchmarks.loc[benchmarks.code == code, "date"]):
            return dict(base, reasons=[f"benchmark_missing_evaluation_sessions:{code}"])
    folds, predicted = [], []
    signal_days = dates[:-1]
    for offset in range(config.train_days, len(signal_days), config.valid_days):
        validation_dates = signal_days[offset:offset + config.valid_days]
        train_dates = signal_days[offset - config.train_days:offset]
        candidates = samples[samples.date.isin(train_dates)]
        train = candidates[(candidates.label_end < validation_dates[0]) & candidates.target.notna()].copy()
        valid = samples[samples.date.isin(validation_dates)].copy()
        if len(train) < max(100, config.min_stocks * 20) or train.target.nunique() < 2:
            raise ValueError(f"第 {len(folds) + 1} 窗训练样本/目标变化不足，不跳过失败窗口")
        if valid.empty:
            raise ValueError(f"第 {len(folds) + 1} 窗没有可预测股票")
        model = _make_model(config, model_factory)
        # 裁剪边界和日期均衡权重只从训练段拟合，不看验证成绩调参。
        low, high = train.target.quantile([0.01, 0.99])
        weight = 1 / train.groupby("date")["code"].transform("count")
        weight = weight / weight.mean()
        fold_start = time.monotonic()
        model.fit(train[FEATURE_COLS], train.target.clip(low, high), sample_weight=weight)
        valid["score"] = model.predict(valid[FEATURE_COLS])
        if not np.isfinite(valid["score"]).all():
            raise ValueError("模型返回非有限预测")
        valid["fold"] = len(folds) + 1
        predicted.append(valid[["date", "code", "score", "target", "net_forward_return", "label_end", "return_20", "fold"]])
        importance = (model.booster_.feature_importance(importance_type="gain") if hasattr(model, "booster_")
                      else getattr(model, "feature_importances_", np.zeros(len(FEATURE_COLS))))
        fold = {
            "fold": len(folds) + 1, "train_start": train.date.min().strftime("%Y-%m-%d"),
            "train_end": train.date.max().strftime("%Y-%m-%d"),
            "train_label_end": train.label_end.max().strftime("%Y-%m-%d"),
            "validation_start": validation_dates[0].strftime("%Y-%m-%d"),
            "validation_end": validation_dates[-1].strftime("%Y-%m-%d"),
            "train_rows": len(train), "purged_or_unlabelled_rows": len(candidates) - len(train),
            "train_symbols": int(train.code.nunique()), "prediction_symbols": int(valid.code.nunique()),
            "prediction_rows": len(valid), "training_target_clip": [float(low), float(high)],
            "fit_predict_seconds": round(time.monotonic() - fold_start, 3),
            "feature_importance": {name: float(value) for name, value in zip(FEATURE_COLS, importance)},
            "prediction_metrics": _prediction_metrics(valid, config.min_stocks),
        }
        folds.append(fold)
        emit({"stage": "fold", "fold": len(folds), "train_rows": len(train), "prediction_rows": len(valid)})
    predictions = pd.concat(predicted, ignore_index=True)
    rebalance_dates = signal_days[config.train_days::config.horizon]
    ml_targets, ml_selections = _targets(predictions, rebalance_dates, config, execution, "score")
    momentum_targets, momentum_selections = _targets(predictions, rebalance_dates, config, execution, "return_20")
    emit({"stage": "portfolios"})
    books = {
        "lightgbm": run_portfolio(bars, ml_targets, execution),
        "momentum20": run_portfolio(bars, momentum_targets, execution),
    }
    etf_execution = replace(execution, max_positions=1, max_weight=1, stamp_tax=0, price_tick=0.001)
    for code in BROAD_ETFS:
        books[code] = run_portfolio(benchmarks[benchmarks.code == code], {rebalance_dates[0]: {code: 1.0}}, etf_execution)
    expected_curve_dates = [day.strftime("%Y-%m-%d") for day in sessions if day > rebalance_dates[0]]
    if any([day["date"] for day in book["curve"]] != expected_curve_dates for book in books.values()):
        raise ValueError("组合日期轴不一致，不能报告不公平的收益比较")
    for fold in folds:
        start = sessions[sessions.index(pd.Timestamp(fold["validation_start"])) + 1].strftime("%Y-%m-%d")
        end = sessions[sessions.index(pd.Timestamp(fold["validation_end"])) + 1].strftime("%Y-%m-%d")
        fold["execution_start"], fold["execution_end"] = start, end
        fold["accounts"] = {name: _slice_metrics(book, start, end, execution.capital) for name, book in books.items()}
    summaries = {name: _book_summary(book) for name, book in books.items()}
    sample_counts = samples.groupby("code").size()
    incomplete = any(summary["stale_mark_days"] for summary in summaries.values())
    execution_status = "incomplete_valuation" if incomplete else "approximate" if execution.mode == "research" else "completed"
    return dict(
        base, status="completed", execution_status=execution_status,
        rows=len(bars), symbols=int(bars.code.nunique()), sample_rows=len(samples),
        sample_symbols=int(samples.code.nunique()),
        sample_coverage=[{"code": code, "raw_rows": len(group),
                          "feature_rows": int(sample_counts.get(code, 0))}
                         for code, group in bars.groupby("code", sort=True)],
        date_axis="independent_calendar" if calendar is not None else "observed_union_not_independent_calendar",
        folds=folds, metrics=summaries, cash_return=0.0,
        excess_total_return={name: summaries["lightgbm"]["total_return"] - value["total_return"]
                             for name, value in summaries.items() if name != "lightgbm"},
        prediction_metrics=_prediction_metrics(predictions, config.min_stocks),
        accounts=books, selections={"lightgbm": ml_selections, "momentum20": momentum_selections},
        predictions=records(predictions.assign(label_end=predictions.label_end.dt.strftime("%Y-%m-%d"))),
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
