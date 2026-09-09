"""日线研究策略的输入校验；只使用截至 as_of 的完整历史窗口。"""
import numpy as np
import pandas as pd


def daily_window(df, bars, as_of=None):
    required = ["date", "open", "high", "low", "close", "volume"]
    if not isinstance(df, pd.DataFrame) or not set(required).issubset(df.columns):
        return None, "缺少 OHLCV 日线"
    frame = df[required].copy()
    try:
        frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
        if as_of is not None:
            frame = frame[frame["date"] <= pd.Timestamp(as_of).normalize()]
        frame = frame.sort_values("date").tail(bars).reset_index(drop=True)
        if len(frame) < bars:
            return None, "历史窗口不足"
        if frame["date"].isna().any() or frame["date"].duplicated().any():
            return None, "日期缺失或重复"
        numeric = frame[required[1:]].apply(pd.to_numeric, errors="raise")
        if not np.isfinite(numeric.to_numpy()).all() or (numeric <= 0).any().any():
            return None, "无效价格、成交量或停牌"
        if ((numeric["high"] < numeric[["open", "close", "low"]].max(axis=1)) |
                (numeric["low"] > numeric[["open", "close", "high"]].min(axis=1))).any():
            return None, "OHLC 关系无效"
        frame[required[1:]] = numeric
    except (ValueError, TypeError, OverflowError):
        return None, "日线格式无效"
    return frame, ""
