"""纯函数数据审计。字段存在不等于来源可信；未知必须保留。"""
import hashlib
import json

import numpy as np
import pandas as pd


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def records(frame):
    data = frame.copy()
    if "date" in data:
        data["date"] = pd.to_datetime(data.date).dt.strftime("%Y-%m-%d")
    return json.loads(data.to_json(orient="records", double_precision=15))


def fingerprint(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def audit_panel(panel, calendar=None, metadata=None, expected_codes=None):
    """calendar 必须来自独立日历，不能用行情日期并集冒充完整交易日历。"""
    metadata = metadata or {}
    frame = pd.DataFrame(panel).copy()
    errors, gaps = [], []
    required = ("date", "code", "open", "high", "low", "close", "volume")
    missing = [key for key in required if key not in frame]
    if missing or frame.empty:
        return {"status": "unavailable", "execution_verified": False,
                "errors": ["empty_or_missing_columns"], "gaps": missing,
                "coverage": [], "input_sha256": fingerprint(records(frame)) if not frame.empty and "date" not in missing else None}
    parsed = pd.to_datetime(frame.date, errors="coerce")
    if parsed.isna().any():
        return {"status": "unavailable", "execution_verified": False,
                "errors": ["invalid_dates"], "gaps": [], "coverage": [], "input_sha256": None}
    frame["date"] = parsed.dt.strftime("%Y-%m-%d")
    if not frame.code.map(lambda v: isinstance(v, str) and len(v) == 6 and v.isdigit()).all():
        errors.append("invalid_security_codes")
    frame["code"] = frame.code.astype(str)
    if frame.duplicated(["date", "code"]).any():
        errors.append("duplicate_bars")
    for key in ("open", "high", "low", "close", "volume"):
        values = pd.to_numeric(frame[key], errors="coerce")
        valid = np.isfinite(values) & (values >= 0 if key == "volume" else values > 0)
        if not valid.all():
            errors.append("invalid_" + key)
        frame[key] = values
    if ((frame.high < frame[["open", "close", "low"]].max(axis=1)) |
            (frame.low > frame[["open", "close", "high"]].min(axis=1))).any():
        errors.append("invalid_ohlc")
    bool_fields = ("tradable", "eligible", "corporate_action")
    for key in bool_fields:
        if key in frame and not frame[key].dropna().map(lambda v: not isinstance(v, str) and v in (True, False, 0, 1)).all():
            errors.append("invalid_" + key)
    if "corporate_action" in frame and frame.corporate_action.fillna(False).astype(bool).any():
        errors.append("corporate_action_unsupported")
    strict_fields = ("raw_open", "raw_close", "tradable", "limit_up", "limit_down")
    validity = {}
    for key in strict_fields + ("eligible", "source", "industry", "market_cap"):
        if key not in frame:
            validity[key] = pd.Series(False, index=frame.index)
        elif key in ("raw_open", "raw_close", "limit_up", "limit_down", "market_cap"):
            value = pd.to_numeric(frame[key], errors="coerce")
            validity[key] = np.isfinite(value) & (value > 0)
        elif key in ("source", "industry"):
            validity[key] = frame[key].notna() & ~frame[key].astype(str).str.strip().isin(("", "unknown"))
        else:
            validity[key] = frame[key].notna()
        if key in strict_fields and not validity[key].all():
            gaps.append("incomplete_" + key)
    if {"limit_up", "limit_down"}.issubset(frame):
        if (pd.to_numeric(frame.limit_up, errors="coerce") <= pd.to_numeric(frame.limit_down, errors="coerce")).any():
            errors.append("invalid_price_limits")
    if calendar is None or not metadata.get("calendar_source"):
        gaps.append("independent_calendar_unknown")
    sessions = sorted(set(str(v) for v in (calendar or [])))
    for day in sessions:
        if pd.Timestamp(day).strftime("%Y-%m-%d") != day:
            raise ValueError("calendar dates must be YYYY-MM-DD")
    start, end = metadata.get("start", frame.date.min()), metadata.get("end", frame.date.max())
    sessions = [day for day in sessions if start <= day <= end]
    if calendar is not None and not sessions:
        gaps.append("calendar_has_no_sessions")
    if calendar is not None and set(frame.date) - set(sessions):
        errors.append("bars_outside_calendar")
    missing_days = sorted(set(sessions) - set(frame.date))
    if missing_days:
        gaps.append("missing_entire_sessions")
    codes = sorted(set(expected_codes or frame.code.unique()))
    coverage = []
    years = sorted(set(day[:4] for day in sessions) | set(frame.date.str[:4]))
    for year in years:
        expected = {d for d in sessions if d.startswith(year)}
        for code in codes:
            part = frame[(frame.code == code) & frame.date.str.startswith(year)]
            absent = sorted(expected - set(part.date))
            if absent:
                gaps.append("missing_security_sessions")
            coverage.append({"year": year, "code": code, "rows": len(part),
                             "expected_sessions": len(expected) if calendar is not None else None,
                             "missing_dates": absent,
                             "fields": {key: float(mask.loc[part.index].mean()) if len(part) else 0.0
                                        for key, mask in validity.items()}})
    if not validity["source"].all() and not metadata.get("source"):
        gaps.append("source_unknown")
    if metadata.get("adjustment") not in ("raw", "qfq", "hfq", "mixed_documented"):
        gaps.append("adjustment_unknown")
    universe = metadata.get("universe", "unknown")
    if universe not in ("point_in_time", "forward_observed"):
        gaps.append("survivorship_bias_unknown")
    if universe == "point_in_time" and (not validity["eligible"].all() or metadata.get("delisted_coverage_verified") is not True):
        gaps.append("historical_membership_or_delisted_coverage_unknown")
    if metadata.get("corporate_actions_verified") is not True:
        gaps.append("corporate_actions_unknown")
    return {"status": "unavailable" if errors else "approximate_only" if gaps else "strict_ready",
            "execution_verified": False, "errors": sorted(set(errors)), "gaps": sorted(set(gaps)),
            "coverage": coverage, "missing_dates": missing_days, "metadata": metadata,
            "input_sha256": fingerprint(records(frame.sort_values(["date", "code"]))),
            "limitations": ["数据声明由提供方负责，字段齐全不证明历史真实性。", "strict_ready 仅表示可进入严格模拟，不能证明可实现收益。"]}
