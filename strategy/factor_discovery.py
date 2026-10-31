"""受限因子发现与类别消融；独立于已冻结的 60 特征模型和交易链路。"""
from dataclasses import asdict, dataclass, replace
import hashlib
from itertools import combinations
import time

import numpy as np
import pandas as pd

from portfolio.factor_backtest import ExecutionConfig, run_portfolio
from research.audit import audit_panel, records
from strategy.etf_research import BROAD_ETFS, validate_etf_panel
from strategy.factor_research import validate_panel
from strategy.ml_research import (
    FEATURE_COLS, LIMITATIONS, MODEL_PARAMS, MLResearchConfig, build_samples,
    _book_summary, _make_model, _prediction_metrics, _slice_metrics, _targets,
)


FEATURE_VERSION = "factor-discovery-ohlcv-v1"
EXTRA_GROUPS = {
    "range_volatility": ("parkinson_20", "garman_klass_20"),
    "volume_pressure": ("cmf_20", "signed_volume_20"),
    "path_risk": ("efficiency_20", "downside_share_20"),
}
EXTRA_COLS = [name for group in EXTRA_GROUPS.values() for name in group]
EXTENDED_COLS = list(FEATURE_COLS) + EXTRA_COLS
VARIANTS = {
    "baseline60": list(FEATURE_COLS),
    "extended66": EXTENDED_COLS,
    **{"without_" + group: [c for c in EXTENDED_COLS if c not in columns]
       for group, columns in EXTRA_GROUPS.items()},
}
SEARCH_INPUTS = ("return_20", "return_60", "volume_ratio_20", "gap_std_20", *EXTRA_COLS)
RANK_TRANSFORM = "per_date_rank_pct_minus_0.5"
INNER_TRAIN_FRACTION = 0.7
MAX_PANEL_ROWS = 300_000
MAX_CANDIDATE_CELLS = 12_000_000
DEFINITIONS = {
    "parkinson_20": "sqrt(mean(log(high/low)^2,20)/(4*log(2)))",
    "garman_klass_20": "sqrt(max(mean(0.5*log(high/low)^2-(2*log(2)-1)*log(close/open)^2,20),0))",
    "cmf_20": "sum(volume*(2*close-high-low)/(high-low),20)/sum(volume,20); flat bar multiplier=0",
    "signed_volume_20": "sum(sign(close-close[-1])*volume,20)/sum(volume,20)",
    "efficiency_20": "(close-close[-20])/sum(abs(diff(close)),20); flat path=0",
    "downside_share_20": "sum(min(return,0)^2,20)/sum(return^2,20); flat path=0.5",
}


@dataclass(frozen=True)
class ResourceBudget:
    max_panel_rows: int = MAX_PANEL_ROWS
    max_candidate_cells: int = MAX_CANDIDATE_CELLS

    def validate(self):
        for name, ceiling in (("max_panel_rows", 1_000_000), ("max_candidate_cells", 48_000_000)):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= ceiling:
                raise ValueError(f"{name} 必须为 1 至 {ceiling} 的整数")


@dataclass(frozen=True)
class SearchConfig:
    max_candidates: int = 48
    max_selected: int = 3
    min_ic_days: int = 15
    min_abs_rank_ic: float = 0.02
    min_sign_stability: float = 0.55
    max_correlation: float = 0.9

    def validate(self):
        for name, low, high in (("max_candidates", 1, 96), ("max_selected", 1, 4), ("min_ic_days", 5, 250)):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError(f"{name} 必须为 {low} 至 {high} 的整数")
        if self.max_selected > self.max_candidates:
            raise ValueError("保留公式数不能超过搜索预算")
        for name in ("min_abs_rank_ic", "min_sign_stability", "max_correlation"):
            value = getattr(self, name)
            if not np.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} 必须在 (0,1] 内")


def feature_manifest():
    return [{"name": name, "category": category, "version": FEATURE_VERSION,
             "required_fields": ["open", "high", "low", "close", "volume"],
             "lookback_bars": 21, "definition": DEFINITIONS[name]}
            for category, names in EXTRA_GROUPS.items() for name in names]


def build_extra_features(bars):
    """仅使用当前及过去 OHLCV；返回无量纲值，成交量压力不等于真实资金流。"""
    opening, high, low, close, volume = (bars[c] for c in ("open", "high", "low", "close", "volume"))
    log_range, log_body = np.log(high / low), np.log(close / opening)
    location = ((2 * close - high - low) / (high - low).replace(0, np.nan)).mask(high == low, 0.0)
    total_volume = volume.rolling(20).sum().replace(0, np.nan)
    change, returns = close.diff(), close.pct_change(fill_method=None)
    path = change.abs().rolling(20).sum()
    variance = returns.pow(2).rolling(20).sum()
    values = {
        "parkinson_20": np.sqrt(log_range.pow(2).rolling(20).mean() / (4 * np.log(2))),
        "garman_klass_20": np.sqrt((0.5 * log_range.pow(2) - (2 * np.log(2) - 1) * log_body.pow(2)).rolling(20).mean().clip(lower=0)),
        "cmf_20": (location * volume).rolling(20).sum() / total_volume,
        "signed_volume_20": (np.sign(change) * volume).rolling(20).sum() / total_volume,
        "efficiency_20": ((close - close.shift(20)) / path.replace(0, np.nan)).mask(path == 0, 0.0),
        "downside_share_20": (returns.clip(upper=0).pow(2).rolling(20).sum() / variance.replace(0, np.nan)).mask(variance == 0, 0.5),
    }
    return pd.DataFrame(values, index=bars.index)[EXTRA_COLS].replace([np.inf, -np.inf], np.nan)


def discovery_samples(bars, sessions, horizon, execution):
    # 保留原基线的样本和标签口径，额外因子不得因未来标签或额外缺失改变对照股票池。
    samples = build_samples(bars, sessions, horizon, execution)
    pieces = []
    for code, group in bars.groupby("code", sort=True):
        daily = group.set_index("date").reindex(pd.DatetimeIndex(sessions))
        available = daily.close.notna() & (daily.volume > 0)
        if "tradable" in daily:
            available &= daily.tradable.eq(True)
        daily.loc[~available, ["open", "high", "low", "close", "volume"]] = np.nan
        extra = build_extra_features(daily)
        extra["date"], extra["code"] = daily.index, code
        pieces.append(extra)
    samples = samples.merge(pd.concat(pieces, ignore_index=True), on=["date", "code"], how="left", validate="one_to_one")
    if not np.isfinite(samples[EXTRA_COLS]).all().all():
        raise ValueError("新增因子在基线样本中不可计算；停止对照，不悄悄删除样本")
    samples[EXTRA_COLS] = (samples.groupby("date")[EXTRA_COLS].rank(pct=True) - 0.5).astype("float32")
    return samples.sort_values(["date", "code"]).reset_index(drop=True)


def formula_catalog(search):
    """固定顺序的白名单表达式，最多三个节点；不执行字符串、外部函数或任意代码。"""
    search.validate()
    # 以固定排列分散各输入的覆盖，排序只由名字决定，与行情和收益无关。
    pairs = list(combinations(SEARCH_INPUTS, 2))
    pairs.sort(key=lambda pair: hashlib.sha256("|".join(pair).encode()).hexdigest())
    result = []
    for left, right in pairs:
        for operator in ("mul", "sub", "add"):
            result.append({"name": f"formula_{len(result) + 1:03d}", "operator": operator,
                           "left": left, "right": right, "nodes": 3,
                           "expression": f"{operator}({left},{right})",
                           "input_transform": RANK_TRANSFORM, "output_transform": RANK_TRANSFORM})
            if len(result) == search.max_candidates:
                return result
    return result


def evaluate_formulas(samples, catalog):
    result = pd.DataFrame(index=samples.index)
    for spec in catalog:
        if (spec["operator"] not in ("add", "sub", "mul") or spec["nodes"] != 3
                or spec["left"] not in SEARCH_INPUTS or spec["right"] not in SEARCH_INPUTS
                or spec.get("input_transform") != RANK_TRANSFORM or spec.get("output_transform") != RANK_TRANSFORM):
            raise ValueError("公式超出白名单或复杂度上限")
        left, right = samples[spec["left"]], samples[spec["right"]]
        result[spec["name"]] = left + right if spec["operator"] == "add" else left - right if spec["operator"] == "sub" else left * right
    return (result.groupby(samples["date"]).rank(pct=True) - 0.5).astype("float32")


def _rank_ic(frame, columns, min_stocks):
    """逐日横截面 RankIC；向量化候选评估，不把股票行数当独立观测数。"""
    daily = []
    for _, group in frame.groupby("date", sort=True):
        group = group.dropna(subset=["target"])
        if len(group) < min_stocks or group.target.nunique() < 2:
            continue
        x, y = group[columns].rank().to_numpy(), group.target.rank().to_numpy()
        x, y = x - x.mean(axis=0), y - y.mean()
        denom = np.sqrt((x * x).sum(axis=0) * (y * y).sum())
        daily.append(np.divide((x * y[:, None]).sum(axis=0), denom,
                               out=np.full(len(columns), np.nan), where=denom > 1e-12))
    result = {}
    for index, name in enumerate(columns):
        values = np.array([row[index] for row in daily])
        values = values[np.isfinite(values)]
        mean = float(values.mean()) if len(values) else None
        result[name] = {"ic_days": len(values), "rank_ic_mean": mean,
                        "sign_stability": float((values * np.sign(mean) > 0).mean()) if len(values) else None}
    return result


def select_formulas(train, train_dates, catalog, search, min_stocks):
    """输入只能是外层训练段；内部发现/选择也按标签最终实现日期隔离。"""
    cutoff = pd.Timestamp(train_dates[int(len(train_dates) * INNER_TRAIN_FRACTION)])
    discovery = train[(train.date < cutoff) & (train.label_end < cutoff)]
    selection = train[train.date >= cutoff]
    names = [item["name"] for item in catalog]
    discovery_metrics = _rank_ic(discovery, names, min_stocks)
    selection_metrics = _rank_ic(selection, names, min_stocks)
    diagnostics, eligible = {}, []
    for name in names:
        first, second = discovery_metrics[name], selection_metrics[name]
        reasons = []
        if min(first["ic_days"], second["ic_days"]) < search.min_ic_days:
            reasons.append("insufficient_inner_ic_days")
        a, b = first["rank_ic_mean"] or 0.0, second["rank_ic_mean"] or 0.0
        if min(abs(a), abs(b)) < search.min_abs_rank_ic:
            reasons.append("weak_inner_rank_ic")
        if a * b <= 0:
            reasons.append("inconsistent_inner_direction")
        if min(first["sign_stability"] or 0.0, second["sign_stability"] or 0.0) < search.min_sign_stability:
            reasons.append("unstable_inner_direction")
        diagnostics[name] = {"discovery": first, "selection": second, "reasons": reasons,
                             "score": min(abs(a), abs(b)), "direction": int(np.sign(a))}
        if not reasons:
            eligible.append(name)
    correlations = discovery[EXTENDED_COLS + eligible].corr() if eligible else None
    selected = []
    for name in sorted(eligible, key=lambda n: (-diagnostics[n]["score"], n)):
        values = correlations.loc[name, EXTENDED_COLS + selected].abs()
        other = values.idxmax() if values.notna().any() else None
        diagnostics[name]["max_discovery_correlation"] = float(values[other]) if other else None
        if other and values[other] >= search.max_correlation:
            diagnostics[name]["reasons"].append("redundant_with:" + other)
        elif len(selected) >= search.max_selected:
            diagnostics[name]["reasons"].append("selection_budget")
        else:
            selected.append(name)
    return {"selected": selected, "candidates": diagnostics, "selection_start": cutoff.strftime("%Y-%m-%d"),
            "discovery_label_end": discovery.label_end.max().strftime("%Y-%m-%d") if len(discovery) else None,
            "selection_label_end": selection.label_end.max().strftime("%Y-%m-%d") if len(selection) else None,
            "purged_inner_rows": int(((train.date < cutoff) & (train.label_end >= cutoff)).sum()),
            "reason": "selected" if selected else "no_formula_passed_inner_gates"}


def _delta(full, reference):
    return {key: full[key] - reference[key] for key in
            ("total_return", "max_drawdown", "mean_daily_turnover", "average_exposure")}


def run_discovery(panel, benchmark_panel, config=None, execution=None, search=None, *,
                  calendar=None, metadata=None, benchmark_metadata=None, expected_codes=None,
                  model_factory=None, progress=None, resources=None):
    """冻结比较规则的历史研究；搜索与调参均不读取外层验证收益。"""
    started = time.monotonic()
    config, search = config or MLResearchConfig(), search or SearchConfig()
    execution = execution or ExecutionConfig(max_positions=config.top_k, max_weight=min(.2, 1 / config.top_k))
    config.validate()
    search.validate()
    resources = resources or ResourceBudget(MAX_PANEL_ROWS, MAX_CANDIDATE_CELLS)
    resources.validate()
    execution.validate()
    if config.top_k > execution.max_positions:
        raise ValueError("top_k 超过账本持仓上限")
    if int(config.train_days * (1 - INNER_TRAIN_FRACTION)) < search.min_ic_days + config.horizon + 1:
        raise ValueError("训练窗口不足以覆盖内部选择、成熟标签及最小 IC 日数")
    if len(panel) > resources.max_panel_rows:
        raise ValueError(f"输入超过研究资源上限 {resources.max_panel_rows} 行；请显式登记资源预算或预先固定研究范围")
    emit = progress or (lambda event: None)
    emit({"stage": "audit"})
    audit = audit_panel(panel, calendar, metadata, expected_codes)
    benchmarks = pd.DataFrame(benchmark_panel).copy()
    if "code" in benchmarks:
        benchmarks = benchmarks[benchmarks.code.isin(BROAD_ETFS)].copy()
    benchmark_audit = audit_panel(benchmarks, calendar, benchmark_metadata, list(BROAD_ETFS))
    catalog = formula_catalog(search)
    base = {"status": "waiting_data", "research_only": True, "promotion_allowed": False,
            "interval_role": "review", "config": asdict(config), "execution": asdict(execution),
            "search_config": asdict(search), "feature_version": FEATURE_VERSION,
            "resource_limits": asdict(resources),
            "features": feature_manifest(), "formula_catalog": catalog,
            "variants": {**VARIANTS, "formula_augmented": "extended66 + inner-selected formulas"},
            "model_params": dict(MODEL_PARAMS, n_jobs=config.threads),
            "data_audit": audit, "benchmark_audit": benchmark_audit,
            "limitations": LIMITATIONS + [
                "公式内部选择仍受多重尝试偏差影响，RankIC 门槛不是显著性证明。",
                "类别消融仅比较固定模型配置下的增量，不证明因果；不按外层结果选冠军。",
                "量价压力是 OHLCV 代理，不是真实资金流；未使用未经核实的成交额或历史行业。",
                "新因子与公式未接入已登记的前向观察，不能改写其 60 特征模型和代码指纹。"]}
    if any(a["status"] == "unavailable" for a in (audit, benchmark_audit)):
        return dict(base, reasons=sorted({r for a in (audit, benchmark_audit) for r in a["errors"]}))
    if execution.mode == "strict" and any(a["status"] != "strict_ready" for a in (audit, benchmark_audit)):
        return dict(base, reasons=["strict_data_not_ready"])
    bars, benchmarks = validate_panel(pd.DataFrame(panel)), validate_etf_panel(benchmarks)
    sessions = sorted(set(bars.date) | set(benchmarks.date))
    if calendar is not None:
        sessions = [pd.Timestamp(d) for d in sorted(set(calendar)) if min(sessions) <= pd.Timestamp(d) <= max(sessions)]
    emit({"stage": "features", "rows": len(bars)})
    samples = discovery_samples(bars, sessions, config.horizon, execution)
    if len(samples) * len(catalog) > resources.max_candidate_cells:
        raise ValueError("样本数乘公式数超过内存预算；请减少预先登记的公式数或研究范围")
    samples = pd.concat([samples, evaluate_formulas(samples, catalog)], axis=1)
    coverage = samples.groupby("date").size()
    available = coverage[coverage >= config.min_stocks]
    if available.empty:
        raise ValueError("没有满足最小股票数的横截面")
    dates = sessions[sessions.index(available.index[0]):]
    if len(dates) < config.train_days + config.valid_days + 1:
        raise ValueError("预热后日期不足以覆盖完整训练、验证和下一开盘")
    for code in BROAD_ETFS:
        if set(dates[config.train_days:]) - set(benchmarks.loc[benchmarks.code == code, "date"]):
            return dict(base, reasons=[f"benchmark_missing_evaluation_sessions:{code}"])
    predictions = {name: [] for name in (*VARIANTS, "formula_augmented")}
    folds = []
    signal_days = dates[:-1]
    for offset in range(config.train_days, len(signal_days), config.valid_days):
        valid_dates = signal_days[offset:offset + config.valid_days]
        train_dates = signal_days[offset - config.train_days:offset]
        candidates = samples[samples.date.isin(train_dates)]
        train = candidates[(candidates.label_end < valid_dates[0]) & candidates.target.notna()].copy()
        valid = samples[samples.date.isin(valid_dates)].copy()
        if len(train) < max(100, config.min_stocks * 20) or train.target.nunique() < 2 or valid.empty:
            raise ValueError("训练/验证覆盖不足；不跳过失败窗口")
        selection = select_formulas(train, train_dates, catalog, search, config.min_stocks)
        features = {**VARIANTS, "formula_augmented": EXTENDED_COLS + selection["selected"]}
        low, high = train.target.quantile([.01, .99])
        weight = 1 / train.groupby("date")["code"].transform("count")
        fold = {"fold": len(folds) + 1, "train_start": train.date.min().strftime("%Y-%m-%d"),
                "train_label_end": train.label_end.max().strftime("%Y-%m-%d"),
                "validation_start": valid_dates[0].strftime("%Y-%m-%d"),
                "validation_end": valid_dates[-1].strftime("%Y-%m-%d"),
                "train_rows": len(train), "purged_outer_rows": len(candidates) - len(train),
                "search": selection, "model_features": features, "prediction_metrics": {}}
        scores_by_features = {}
        for name, columns in features.items():
            key = tuple(columns)
            if key not in scores_by_features:
                model = _make_model(config, model_factory)
                model.fit(train[columns], train.target.clip(low, high), sample_weight=weight / weight.mean())
                score = model.predict(valid[columns])
                if not np.isfinite(score).all():
                    raise ValueError("模型产生非有限预测")
                scores_by_features[key] = score
            part = valid[["date", "code", "target", "net_forward_return", "label_end", "return_20"]].copy()
            part["score"], part["fold"] = scores_by_features[key], fold["fold"]
            predictions[name].append(part)
            fold["prediction_metrics"][name] = _prediction_metrics(part, config.min_stocks)
        folds.append(fold)
        emit({"stage": "fold", "fold": fold["fold"], "selected_formulas": selection["selected"]})
    predictions = {name: pd.concat(parts, ignore_index=True) for name, parts in predictions.items()}
    rebalance_dates = signal_days[config.train_days::config.horizon]
    selections, books = {}, {}
    emit({"stage": "portfolios"})
    for name, frame in {**predictions, "momentum20": predictions["baseline60"]}.items():
        targets, selections[name] = _targets(frame, rebalance_dates, config, execution, "return_20" if name == "momentum20" else "score")
        books[name] = run_portfolio(bars, targets, execution)
    etf_execution = replace(execution, max_positions=1, max_weight=1, stamp_tax=0, price_tick=.001)
    for code in BROAD_ETFS:
        books[code] = run_portfolio(benchmarks[benchmarks.code == code], {rebalance_dates[0]: {code: 1.0}}, etf_execution)
    expected_dates = [d.strftime("%Y-%m-%d") for d in sessions if d > rebalance_dates[0]]
    if any([d["date"] for d in book["curve"]] != expected_dates for book in books.values()):
        raise ValueError("账户日期轴不一致，拒绝比较")
    for fold in folds:
        start = sessions[sessions.index(pd.Timestamp(fold["validation_start"])) + 1].strftime("%Y-%m-%d")
        end = sessions[sessions.index(pd.Timestamp(fold["validation_end"])) + 1].strftime("%Y-%m-%d")
        fold["accounts"] = {name: _slice_metrics(book, start, end, execution.capital) for name, book in books.items()}
    metrics = {name: _book_summary(book) for name, book in books.items()}
    incomplete = any(m["stale_mark_days"] for m in metrics.values())
    increments = {"extra_groups_vs_baseline60": _delta(metrics["extended66"], metrics["baseline60"]),
                  "formulas_vs_extended66": _delta(metrics["formula_augmented"], metrics["extended66"]),
                  **{group: _delta(metrics["extended66"], metrics["without_" + group]) for group in EXTRA_GROUPS}}
    return dict(base, status="completed", execution_status="incomplete_valuation" if incomplete else "approximate" if execution.mode == "research" else "completed",
                rows=len(bars), symbols=int(bars.code.nunique()), sample_rows=len(samples), folds=folds,
                metrics=metrics, increments=increments, cash_return=0.0, accounts=books, selections=selections,
                predictions={name: records(frame.assign(label_end=frame.label_end.dt.strftime("%Y-%m-%d"))) for name, frame in predictions.items()},
                elapsed_seconds=round(time.monotonic() - started, 3))
