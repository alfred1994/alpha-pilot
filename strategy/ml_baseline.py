"""离线随机森林研究基线：因果特征、时间验证和 T+1 理想成交评估。

不写模型、不修改模拟账户、不调用行情或 LLM。收益只是日线研究估计，
未模拟停牌、涨跌停、整手和最低佣金，不能作为实盘或主交易链路业绩。
"""
import hashlib
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from strategy.qlib_signal import _compute_atr, _compute_macd


FEATURE_COLS = [
    "return_5", "return_10", "return_20",
    "close_ma5", "close_ma20", "close_ma60",
    "bollinger_width", "atr14_ratio", "volatility_20",
    "volume_ratio", "rsi14", "macd_dif_ratio", "macd_dea_ratio",
    "macd_hist_ratio",
]
MODEL_PARAMS = dict(
    n_estimators=100, max_depth=5, min_samples_leaf=10,
    max_features="sqrt", random_state=42, n_jobs=1,
)
BAR_COLS = ["date", "open", "high", "low", "close", "volume"]


def load_local_bars(code, start_date, end_date, db_path=None):
    """只读连接，不经过 Database 初始化迁移或行情下载。日期范围含预热段。"""
    if db_path is None:
        from config import DATA_DIR
        db_path = Path(DATA_DIR) / "quant.db"
    if pd.Timestamp(start_date) > pd.Timestamp(end_date):
        raise ValueError("开始日期不能晚于结束日期")
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return pd.read_sql_query(
            "SELECT date, open, high, low, close, volume FROM k_daily "
            "WHERE code = ? AND date >= ? AND date <= ? ORDER BY date",
            connection, params=(code, start_date, end_date),
        )
    finally:
        connection.close()


def _validated_bars(df):
    if not set(BAR_COLS).issubset(df.columns) or df.empty:
        raise ValueError("需要非空的 date/open/high/low/close/volume 日线")
    bars = df[BAR_COLS].copy().reset_index(drop=True)
    bars["date"] = pd.to_datetime(bars["date"], errors="raise")
    if bars["date"].isna().any() or bars["date"].duplicated().any():
        raise ValueError("日线日期缺失或重复")
    if (bars["date"] != bars["date"].dt.normalize()).any():
        raise ValueError("输入必须是日线日期，不能包含盘中时间")
    if not bars["date"].is_monotonic_increasing:
        raise ValueError("日线必须按日期严格递增，禁止乱序切分")
    bars[BAR_COLS[1:]] = bars[BAR_COLS[1:]].apply(pd.to_numeric, errors="raise")
    values = bars[BAR_COLS[1:]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("OHLCV 必须有限且为正；停牌/零量数据需先核验，不能默默填充")
    if ((bars["high"] < bars[["open", "close", "low"]].max(axis=1)).any()
            or (bars["low"] > bars[["open", "close"]].min(axis=1)).any()):
        raise ValueError("OHLC 高低价关系异常")
    return bars


def build_baseline_features(df):
    """14 个无量纲特征；第 t 行只使用 t 日及之前的数据。"""
    bars = _validated_bars(df)
    close = bars["close"]
    features = pd.DataFrame(index=bars.index)
    for days in (5, 10, 20):
        features[f"return_{days}"] = close.pct_change(days, fill_method=None)
    for days in (5, 20, 60):
        features[f"close_ma{days}"] = close / close.rolling(days).mean() - 1
    features["bollinger_width"] = 4 * close.rolling(20).std() / close.rolling(20).mean()
    features["atr14_ratio"] = _compute_atr(bars["high"], bars["low"], close) / close
    features["volatility_20"] = close.pct_change(fill_method=None).rolling(20).std()
    features["volume_ratio"] = bars["volume"] / bars["volume"].rolling(20).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    # 单边上涨为 1，单边下跌为 0，平盘为 0.5，避免零分母抹掉全部样本。
    features["rsi14"] = (gain / (gain + loss)).where(gain + loss != 0, 0.5)
    dif, dea, hist = _compute_macd(close)
    for name, value in (("dif", dif), ("dea", dea), ("hist", hist)):
        features[f"macd_{name}_ratio"] = value / close
    return features[FEATURE_COLS]


def build_samples(df):
    bars = _validated_bars(df)
    features = build_baseline_features(bars)
    samples = features.copy()
    samples["date"] = bars["date"]
    samples["entry_date"] = bars["date"].shift(-1)
    samples["label_end"] = bars["date"].shift(-2)
    # t 收盘产生信号，t+1 开盘买入，最早 t+2 开盘卖出，符合 A 股 T+1。
    samples["forward_return"] = bars["open"].shift(-2) / bars["open"].shift(-1) - 1
    samples = samples.iloc[59:-2].copy()
    if samples.empty or not np.isfinite(samples[FEATURE_COLS + ["forward_return"]]).all().all():
        raise ValueError("指标预热后无有效样本或存在缺失值；至少需要 62 根完整日线")
    samples["target"] = (samples["forward_return"] > 0).astype(int)
    return samples.reset_index(drop=True)


def _classification(y, probability):
    from sklearn.metrics import roc_auc_score

    y = np.asarray(y)
    prediction = (probability >= 0.5).astype(int)
    recalls = [float(np.mean(prediction[y == label] == label))
               for label in (0, 1) if np.any(y == label)]
    both_classes = len(recalls) == 2
    return {
        "accuracy": float(np.mean(prediction == y)),
        "balanced_accuracy": float(np.mean(recalls)) if both_classes else None,
        "auc": float(roc_auc_score(y, probability)) if both_classes else None,
        "brier_score": float(np.mean((probability - y) ** 2)),
    }


def _performance(returns, positions, commission, slippage, stamp_tax):
    """每行是相邻开盘之间的持仓；跨折不清仓，仅在整个 OOS 结束时结算。"""
    positions = np.asarray(positions, dtype=int)
    previous = np.r_[0, positions[:-1]]
    buys = (positions == 1) & (previous == 0)
    sells = (positions == 0) & (previous == 1)
    factors = 1 + positions * np.asarray(returns)
    # 买入滑点与佣金减少可买份额；卖出滑点、佣金、印花税减少所得现金。
    buy_factor = 1 / ((1 + slippage) * (1 + commission))
    sell_factor = (1 - slippage) * (1 - commission - stamp_tax)
    factors[buys] *= buy_factor
    factors[sells] *= sell_factor
    if positions[-1]:
        factors[-1] *= sell_factor
    equity = np.r_[1.0, np.cumprod(factors)]
    daily = factors - 1
    std = float(daily.std(ddof=1)) if len(daily) > 1 else 0.0
    return {
        "total_return": float(equity[-1] - 1),
        "max_drawdown": float(np.min(equity / np.maximum.accumulate(equity) - 1)),
        "sharpe_252_observations": float(daily.mean() / std * np.sqrt(252)) if std > 0 else None,
        "exposure": float(positions.mean()),
        "entries": int(buys.sum()),
        "exits": int(sells.sum() + positions[-1]),
    }


def run_baseline(df, train_days=250, valid_days=60, threshold=0.55,
                 commission=0.0003, slippage=0.001, stamp_tax=0.0005,
                 validation="walk_forward"):
    """固定超参数的单股离线实验，不使用验证集挑阈值或选择特征。"""
    import sklearn
    from sklearn.ensemble import RandomForestClassifier

    if (isinstance(train_days, bool) or not isinstance(train_days, int) or train_days < 60
            or isinstance(valid_days, bool) or not isinstance(valid_days, int) or valid_days < 1):
        raise ValueError("训练窗至少 60 个样本日，验证窗为正整数")
    if validation not in ("walk_forward", "holdout"):
        raise ValueError("validation 必须是 walk_forward 或 holdout")
    if not np.isfinite(threshold) or not 0 < threshold < 1:
        raise ValueError("概率阈值必须在 (0, 1) 内")
    if any(not np.isfinite(v) or not 0 <= v < 1 for v in (commission, slippage, stamp_tax)):
        raise ValueError("费用率必须为 [0, 1) 内的有限数")
    if commission + stamp_tax >= 1:
        raise ValueError("卖出总费用必须小于 1")
    bars = _validated_bars(df)
    samples = build_samples(bars)
    if len(samples) < train_days + valid_days:
        raise ValueError(f"有效样本 {len(samples)} 不足：需训练 {train_days} + 验证 {valid_days}")
    folds, predictions = [], []
    step = valid_days if validation == "walk_forward" else len(samples)
    for start in range(train_days, len(samples), step):
        end = min(start + valid_days, len(samples)) if validation == "walk_forward" else len(samples)
        valid = samples.iloc[start:end]
        train = samples.iloc[start - train_days:start]
        # 按标签真正实现的时间清除边界，而非先 dropna 后猜测 gap 行数。
        train = train[train["label_end"] < valid["date"].iloc[0]]
        model = RandomForestClassifier(**MODEL_PARAMS)
        model.fit(train[FEATURE_COLS], train["target"])
        class_one = np.flatnonzero(model.classes_ == 1)
        probabilities = (model.predict_proba(valid[FEATURE_COLS])[:, class_one[0]]
                         if len(class_one) else np.zeros(len(valid)))
        prior = float(train["target"].mean())
        baseline = np.full(len(valid), prior)
        importance = dict(zip(FEATURE_COLS, map(float, model.feature_importances_)))
        fold = {
            "fold": len(folds) + 1,
            "train_start": train["date"].iloc[0].strftime("%Y-%m-%d"),
            "train_end": train["date"].iloc[-1].strftime("%Y-%m-%d"),
            "train_label_end": train["label_end"].iloc[-1].strftime("%Y-%m-%d"),
            "validation_start": valid["date"].iloc[0].strftime("%Y-%m-%d"),
            "validation_end": valid["date"].iloc[-1].strftime("%Y-%m-%d"),
            "train_rows": len(train), "validation_rows": len(valid),
            "purged_rows": train_days - len(train), "training_up_frequency": prior,
            "model": _classification(valid["target"], probabilities),
            "prior_baseline": _classification(valid["target"], baseline),
            "feature_importance": dict(sorted(importance.items(), key=lambda item: -item[1])),
        }
        folds.append(fold)
        output = valid[["date", "entry_date", "label_end", "forward_return", "target"]].copy()
        output["probability"] = probabilities
        output["prior_probability"] = baseline
        output["fold"] = fold["fold"]
        predictions.append(output)
    oos = pd.concat(predictions, ignore_index=True)
    positions = (oos["probability"].to_numpy() >= threshold).astype(int)
    costs = (commission, slippage, stamp_tax)
    metrics = _performance(oos["forward_return"], positions, *costs)
    for col in ("date", "entry_date", "label_end"):
        oos[col] = oos[col].dt.strftime("%Y-%m-%d")
    oos["position"] = positions
    from research.audit import audit_panel
    audit_bars = df.copy()
    if "code" not in audit_bars:
        audit_bars["code"] = df.attrs.get("code", "unknown")
    data_audit = audit_panel(audit_bars, df.attrs.get("calendar"), df.attrs.get("metadata"))
    return {
        "status": "research_only", "model": "random_forest_v1", "data_audit": data_audit,
        "library_versions": {"numpy": np.__version__, "pandas": pd.__version__, "sklearn": sklearn.__version__},
        "validation": validation, "features": FEATURE_COLS, "model_params": MODEL_PARAMS.copy(),
        "input_sha256": hashlib.sha256(bars.to_csv(index=False).encode("utf-8")).hexdigest(),
        "input_rows": len(bars), "sample_rows": len(samples), "oos_rows": len(oos),
        "train_days": train_days, "valid_days": valid_days, "threshold": threshold,
        "costs": {"commission_per_side": commission, "slippage_per_side": slippage, "sell_stamp_tax": stamp_tax},
        "classification": _classification(oos["target"], oos["probability"].to_numpy()),
        "prior_baseline": _classification(oos["target"], oos["prior_probability"].to_numpy()),
        "strategy_net": metrics,
        "strategy_gross": _performance(oos["forward_return"], positions, 0, 0, 0),
        "buy_hold_net": _performance(oos["forward_return"], np.ones(len(oos), dtype=int), *costs),
        "cash_return": 0.0,
        "folds": folds, "predictions": oos.to_dict(orient="records"),
        "limitations": [
            "仅日线理想成交研究，不是自动交易系统或实盘收益，不自动晋级模型。",
            "信号于 t 收盘形成，t+1 开盘调仓，持有至少到 t+2 开盘；跨折保持仓位，末尾扣费清仓。",
            "未模拟涨跌停、停牌成交失败、整手、最低佣金、市场冲击和现金利息。",
            "输入来源、复权口径、交易日缺口与历史股票池需独立核验；选股存在幸存者偏差。",
            "未验证特征有效性或保证盈利；查看 OOS 后反复调参会污染验证，需保留新的最终留出期。",
            "Sharpe 按每年 252 个观测区间估算，单类验证窗 AUC/平衡准确率为 null。",
            "特征重要性为训练集树分裂重要性，不是因果关系或收益贡献。",
        ],
    }
