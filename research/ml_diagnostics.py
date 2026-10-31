"""固定预测的事后诊断；不重新选模型、不更改旧收益或推断因果。"""
import numpy as np
import pandas as pd

from portfolio.factor_backtest import ExecutionConfig, run_portfolio
from strategy.ml_research import FEATURE_COLS, build_samples
from strategy.factor_research import validate_panel


def parse_calendar(payload, start, end):
    """只接受完整的 Baostock 原始日历响应，不走工作日/节假日猜测降级。"""
    if not payload or payload.get("error_code") != "0":
        raise ValueError("独立交易日历查询失败")
    rows = payload.get("data") or []
    dates = []
    for row in rows:
        if len(row) != 2 or row[1] not in ("0", "1") or pd.Timestamp(row[0]).strftime("%Y-%m-%d") != row[0]:
            raise ValueError("交易日历记录格式错误")
        dates.append(row[0])
        if row[1] == "1" and pd.Timestamp(row[0]).dayofweek >= 5:
            raise ValueError("A股日历包含周末交易日")
    expected = pd.date_range(start, end).strftime("%Y-%m-%d").tolist()
    if len(dates) != len(set(dates)) or not set(expected).issubset(dates):
        raise ValueError("独立日历日期不完整或重复")
    return sorted(row[0] for row in rows if row[1] == "1" and start <= row[0] <= end)


def daily_rank_ic(frame, columns):
    valid = frame.dropna(subset=["target"])
    rows = []
    for date, cross in valid.groupby("date", sort=True):
        if len(cross) < 5 or cross.target.nunique() < 2:
            continue
        target = cross.target.rank()
        row = {"date": pd.Timestamp(date).strftime("%Y-%m-%d"), "fold": int(cross.fold.iloc[0]), "stocks": len(cross)}
        row.update({col: float(cross[col].rank().corr(target)) if cross[col].nunique() > 1 else None for col in columns})
        rows.append(row)
    return pd.DataFrame(rows)


def audit_stale_marks(accounts, evidence):
    """第三方停牌证据仅用于分类，不将复权旧价改成可成交原价。"""
    status = {}
    for payload in evidence:
        if not payload or payload.get("error_code") != "0":
            continue
        fields = payload.get("fields", [])
        if not {"date", "code", "tradestatus"}.issubset(fields):
            continue
        for row in payload.get("data", []):
            data = dict(zip(fields, row))
            code = data["code"].split(".")[-1]
            key = (str(data["date"]), code)
            value = data["tradestatus"]
            if key in status and status[key] != value:
                raise ValueError("停牌证据相互冲突")
            status[key] = value
    result = {}
    for name, book in accounts.items():
        events = []
        for day in book["curve"]:
            for code in day["stale_marks"]:
                state = status.get((day["date"], code))
                events.append({"date": day["date"], "code": code,
                               "classification": "provider_confirmed_suspension" if state == "0" else
                                   "provider_reports_trading" if state == "1" else "unresolved",
                               "source": "baostock_tradestatus" if state is not None else "unknown"})
        result[name] = {
            "stale_days": len({e["date"] for e in events}), "events": events,
            "confirmed_suspended_events": sum(e["classification"] == "provider_confirmed_suspension" for e in events),
            "unresolved_events": sum(e["classification"] == "unresolved" for e in events),
            "valuation_still_approximate": bool(events),
        }
    return result


def diagnose(panel, predictions, report, accounts, calendar, suspension_evidence):
    bars = validate_panel(panel)
    observed = set(bars.date.dt.strftime("%Y-%m-%d"))
    independent = {day for day in calendar if min(observed) <= day <= max(observed)}
    outside = sorted(observed - independent)
    if outside:
        raise ValueError("行情包含独立日历之外日期")
    execution = ExecutionConfig(**report["execution"])
    samples = build_samples(bars, sorted(pd.Timestamp(d) for d in independent), report["config"]["horizon"], execution)
    predictions = predictions.copy()
    predictions["date"] = pd.to_datetime(predictions.date)
    # 只使用已冻结预测；重新计算的因子用于解释，不重训或选择最优收益。
    joined = predictions[["date", "code", "score", "fold"]].merge(samples, on=["date", "code"], validate="one_to_one")
    if len(joined) != len(predictions):
        raise ValueError("冻结预测与输入特征覆盖不一致")
    original = predictions.set_index(["date", "code"])["target"].sort_index()
    rebuilt = joined.set_index(["date", "code"])["target"].sort_index()
    if not np.allclose(original, rebuilt, equal_nan=True, rtol=1e-9, atol=1e-12):
        raise ValueError("标签重算与冻结结果不一致，不能进行归因")
    ic = daily_rank_ic(joined, ["score"] + FEATURE_COLS)
    per_fold = []
    for fold, group in joined.groupby("fold", sort=True):
        daily = ic[ic.fold == fold]
        usable = group[group.target.notna()]
        pending = group.label_end.isna()
        exposure = []
        spreads = []
        for _, cross in usable.groupby("date"):
            top = cross.nlargest(report["config"]["top_k"], "score")
            exposure.append(top[FEATURE_COLS].mean() - cross[FEATURE_COLS].mean())
            ranks = cross.score.rank(pct=True)
            spreads.append(float(cross.loc[ranks >= .8, "target"].mean() - cross.loc[ranks <= .2, "target"].mean()))
        mean_exposure = pd.DataFrame(exposure).mean()
        rank_correlation = {name: float(usable.score.corr(usable[name])) for name in ("volatility_60", "gap_std_60", "return_20")}
        per_fold.append({
            "fold": int(fold), "prediction_rows": len(group), "labelled_rows": len(usable),
            "pending_horizon_rows": int(pending.sum()),
            "missing_realized_label_rows": int((group.target.isna() & ~pending).sum()),
            "mean_daily_rank_ic": float(daily.score.mean()),
            "top_minus_bottom_daily_excess": float(np.mean(spreads)),
            "feature_rank_ic": {name: float(daily[name].mean()) if daily[name].notna().any() else None for name in FEATURE_COLS},
            "top_selection_exposure": {name: float(mean_exposure[name]) for name in FEATURE_COLS},
            "pooled_score_feature_correlation_descriptive": rank_correlation,
        })
    early, late = ic[ic.fold <= ic.fold.max()-2], ic[ic.fold > ic.fold.max()-2]
    reversals = []
    for name in FEATURE_COLS:
        before, after = early[name].mean(), late[name].mean()
        if np.isfinite(before) and np.isfinite(after):
            reversals.append({"factor": name, "early_rank_ic": float(before), "late_rank_ic": float(after),
                              "sign_reversed": bool(before * after < 0)})
    return {
        "status": "completed", "interval_role": "posthoc_diagnostic", "promotion_allowed": False,
        "calendar_audit": {"sessions": len(independent), "missing_entire_sessions": sorted(independent-observed),
                           "outside_calendar": outside, "source": "baostock_raw_trade_dates"},
        "folds": per_fold, "factor_regime_comparison": reversals,
        "stale_mark_audit": audit_stale_marks(accounts, suspension_evidence),
        "limitations": ["这是看过结果后的描述性诊断，不是独立因果检验，也不用于改写旧回测。",
                        "停牌证据不能补足真实涨跌停、公司行动、历史股票池及原价成交元数据。",
                        "末端标签未成熟与历史缺报价分开统计；不按事后数据缺失删除已产生的信号。"],
    }
