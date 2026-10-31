"""指数基准行情：显式指数身份，不套用股票复权和股数成交量校验。"""
import math


def load_hs300_history(date, *, persist=False):
    from data.kt_realtime import get_kt_client
    frame = get_kt_client().get_kline("sh000300", period="d", count=1000)
    if frame is None or frame.empty:
        return None
    frame = frame.loc[frame["data_date"].astype(str) <= date].copy()
    if frame.empty or frame["data_date"].duplicated().any():
        return None
    if not frame["close"].map(lambda v: math.isfinite(float(v)) and float(v) > 0).all():
        return None
    frame = frame.sort_values("data_date").rename(columns={"data_date": "date"})
    if persist:
        from data.database import Database
        # 指数没有可换算为股数的成交量，保持缺失，不伪造为股票成交量。
        rows = [{"code": "000300", "date": row["date"],
                 **{key: row[key] for key in ("open", "high", "low", "close")},
                 "volume": None, "amount": None} for row in frame.to_dict("records")]
        with Database() as db:
            db.insert_k_daily(rows, source="tencent_index")
    return frame
