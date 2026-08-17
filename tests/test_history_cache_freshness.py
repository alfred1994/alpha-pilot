#!/usr/bin/env python3
"""历史K线缓存新鲜度回归测试。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data.database as database
from data.database import Database
from data.history import _to_bs_code, _to_system_code, _try_cache
from strategy.market_regime import _calc_trend_indicators


def main():
    assert _to_bs_code("000300.SH") == "sh.000300"
    assert _to_bs_code("sh.000300") == "sh.000300"
    assert _to_system_code("000300.SH") == "000300"
    assert _to_system_code("sh.000300") == "000300"

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()
    os.unlink(db_path)
    old_db_path = database.DB_PATH
    database.DB_PATH = db_path
    try:
        with Database(db_path=db_path) as db:
            db.insert_k_daily([{
                "code": "000300",
                "date": "2026-06-26",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
                "amount": 1,
                "turn": 1,
                "pctChg": 0,
            }], source="test")

        fresh = _try_cache("000300", "20240101", "2026-08-18")
        assert fresh is None, "明显过期缓存不能直接作为新鲜行情返回"
        stale = _try_cache("000300", "20240101", "2026-08-18", allow_stale=True)
        assert stale is not None, "外部数据源失败时仍可回退到过期缓存"
        assert stale.attrs["stale_cache_days"] > 3, "过期缓存必须标记滞后天数"

        import data.history as history
        original_daily = history.get_daily
        history.get_daily = lambda *args, **kwargs: stale
        try:
            indicators = _calc_trend_indicators()
            assert indicators.get("trend_data_source") == "stale_cache"
            assert "hs300_pct_5d" not in indicators
        finally:
            history.get_daily = original_daily
        print("历史K线缓存新鲜度测试通过")
    finally:
        database.DB_PATH = old_db_path
        for path in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    main()
