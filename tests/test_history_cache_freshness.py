#!/usr/bin/env python3
"""历史K线缓存新鲜度回归测试。"""
import os
import sys
import tempfile
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data.database as database
from data.database import Database
from data.history import _to_bs_code, _to_system_code, _try_cache, get_daily
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
            db.insert_k_daily([
                {"code": "000001", "date": "2026-06-01", "open": 1, "high": 1,
                 "low": 1, "close": 1, "volume": 1, "amount": 1, "turn": 1, "pctChg": 0},
                {"code": "000001", "date": "2026-08-18", "open": 1, "high": 1,
                 "low": 1, "close": 1, "volume": 1, "amount": 1, "turn": 1, "pctChg": 0},
            ], source="test")

        fresh = _try_cache("000300", "20240101", "2026-08-18")
        assert fresh is None, "明显过期缓存不能直接作为新鲜行情返回"
        stale = _try_cache("000300", "20240101", "2026-08-18", allow_stale=True)
        assert stale is not None, "外部数据源失败时仍可回退到过期缓存"
        assert stale.attrs["stale_cache_days"] > 3, "过期缓存必须标记滞后天数"
        assert _try_cache("000001", "20260601", "20260818", require_full_range=True) is None, \
            "内部严重缺口的缓存不能满足完整研究区间"

        import data.history as history

        # 离线确定性交易日历：直接用本地节假日表构建，避免测试触碰 Baostock。
        from scheduler.market_calendar import _fallback_calendar_with_holidays
        history._trading_calendar_cache = {
            year: _fallback_calendar_with_holidays(year)
            for year in range(2020, 2027)
        }

        original_daily = history.get_daily
        history.get_daily = lambda *args, **kwargs: stale
        try:
            indicators = _calc_trend_indicators()
            assert indicators.get("trend_data_source") == "stale_cache"
            assert "hs300_pct_5d" not in indicators
        finally:
            history.get_daily = original_daily

        # 单条旧数据非空也不能冒充完整历史：同花顺覆盖不足时必须继续回退。
        old_hithink = pd.DataFrame({
            "date": ["2020-01-02"], "open": [10], "high": [11], "low": [9], "close": [10],
        })
        full_dates = pd.date_range("2020-01-02", "2026-09-09", freq="B").strftime("%Y-%m-%d")
        full_longport = pd.DataFrame({
            "date": full_dates,
            "open": 10, "high": 11, "low": 9, "close": 10,
        })
        with patch("data.history._try_cache", return_value=None), \
                patch("data.history._try_hithink", return_value=old_hithink), \
                patch("data.history._try_kt_daily", return_value=None), \
                patch("data.history._try_longbridge", return_value=old_hithink) as longport, \
                patch("data.history._fetch_baostock", return_value=full_longport) as baostock, \
                patch("data.history._save_to_cache"):
            covered = get_daily("600519", "2020-01-01", "2026-09-09", require_full_range=True)
            assert covered.attrs["source"] == "baostock", "合格Baostock结果不能被前序降级帧覆盖"
            assert covered.attrs["coverage_status"] == "ok", "完整端点覆盖才可标记成功"
            longport.assert_called_once()
            baostock.assert_called_once()

        # 所有来源都不足时保留可诊断的 stale 标记，不能返回伪造的 ok。
        with patch("data.history._try_cache", return_value=None), \
                patch("data.history._try_hithink", return_value=old_hithink), \
                patch("data.history._try_kt_daily", return_value=None), \
                patch("data.history._try_longbridge", return_value=old_hithink), \
                patch("data.history._fetch_baostock", return_value=old_hithink), \
                patch("data.history._save_to_cache") as save:
            incomplete = get_daily("600519", "2020-01-01", "2026-09-09", require_full_range=True)
            assert incomplete.attrs["coverage_status"] == "stale", "过期结果必须显式标记stale"
            assert incomplete.attrs["coverage_end_gap_days"] > 10, "端点缺口需要被保留"
            save.assert_not_called()

        invalid_price = old_hithink.astype({"close": float})
        invalid_price.loc[0, "close"] = float("inf")
        assert history._assess_daily_coverage(
            invalid_price, "2020-01-01", "2020-01-02"
        ) is None, "无穷价格不能通过日线有效性校验"
        sparse = pd.DataFrame({
            "date": ["2020-01-02", "2026-09-09"],
            "open": [10, 10], "high": [11, 11], "low": [9, 9], "close": [10, 10],
        })
        sparse_status = history._assess_daily_coverage(sparse, "2020-01-01", "2026-09-09")
        assert sparse_status.attrs["coverage_status"] == "incomplete", "多年稀疏日线不能标记完整"
        short_sparse = pd.DataFrame({
            "date": ["2026-09-05"],
            "open": [10], "high": [11], "low": [9], "close": [10],
        })
        short_status = history._assess_daily_coverage(short_sparse, "2026-09-01", "2026-09-10")
        assert short_status.attrs["coverage_status"] == "incomplete", \
            "短窗口单条数据必须按完整请求窗口密度判定"
        assert short_status.attrs["coverage_expected_weekdays"] == 8, \
            "短窗口密度分母为请求窗口工作日数"
        friday_only = pd.DataFrame({
            "date": ["2026-09-04"],
            "open": [10], "high": [11], "low": [9], "close": [10],
        })
        weekend_status = history._assess_daily_coverage(friday_only, "2026-09-04", "2026-09-07")
        assert weekend_status.attrs["coverage_status"] == "ok", \
            "周五至周一仅有周五完成日线不应被误拒"

        # 交易日间隙(动态节假日)：2026春节休市(2/15-2/23)跨越11个日历日，
        # 超过10天日历容忍窗，但交易日间隙为0，不得误判为数据空洞。
        spring_festival = pd.DataFrame({
            "date": ["2026-02-11", "2026-02-12", "2026-02-13", "2026-02-24", "2026-02-25"],
            "open": [10, 10, 10, 10, 10], "high": [11, 11, 11, 11, 11],
            "low": [9, 9, 9, 9, 9], "close": [10, 10, 10, 10, 10],
        })
        holiday_status = history._assess_daily_coverage(
            spring_festival, "2026-02-11", "2026-02-25")
        assert holiday_status.attrs["coverage_status"] == "ok", \
            (f"春节休市不得误判incomplete (got {holiday_status.attrs['coverage_status']}, "
             f"density={holiday_status.attrs['coverage_observed_density']:.2f})")
        assert holiday_status.attrs["coverage_max_internal_gap_trading_days"] == 0, \
            "春节休市的交易日间隙必须为0(动态节假日生效)"

        # 真实空洞仍拦截：5/26-6/19整段缺失(19个工作日，扣除端午后18个交易日)
        all_weekdays = list(pd.bdate_range("2026-05-04", "2026-07-31").strftime("%Y-%m-%d"))
        hole_dates = [d for d in all_weekdays if not ("2026-05-26" <= d <= "2026-06-19")]
        hole_frame = pd.DataFrame({
            "date": hole_dates,
            "open": 10, "high": 11, "low": 9, "close": 10,
        })
        hole_status = history._assess_daily_coverage(hole_frame, "2026-05-04", "2026-07-31")
        assert hole_status.attrs["coverage_status"] == "incomplete", \
            "超过两周的数据空洞必须被拦截"
        assert hole_status.attrs["coverage_max_internal_gap_trading_days"] > 10, \
            (f"交易日间隙必须超过容忍度 (got "
             f"{hole_status.attrs['coverage_max_internal_gap_trading_days']})")
        assert hole_status.attrs["coverage_observed_density"] >= history.HISTORY_COVERAGE_MIN_DENSITY, \
            "密度未走低，证明拦截来自交易日间隙规则而非密度"

        # 腾讯KT日线适配器：前复权数据映射为统一 schema
        from data.kt_realtime import KTRealtimeClient
        kt_raw = pd.DataFrame({
            "code": ["600519"] * 3,
            "symbol": ["sh600519"] * 3,
            "sec_type": ["stock"] * 3,
            "datetime": ["2026-09-22", "2026-09-23", "2026-09-24"],
            "open": [10.0, 10.2, 10.4],
            "close": [10.2, 10.4, 10.6],
            "high": [10.3, 10.5, 10.7],
            "low": [9.9, 10.1, 10.3],
            "volume_raw": [100000.0, 110000.0, 120000.0],
            "volume_hand": [1000.0, 1100.0, 1200.0],
            "amount_yuan": [float("nan")] * 3,
            "source": ["tencent_gtimg"] * 3,
            "received_at": ["2026-09-24T15:00:00"] * 3,
            "data_date": ["2026-09-22", "2026-09-23", "2026-09-24"],
            "adjustment": ["qfq"] * 3,
            "amount_status": ["missing"] * 3,
            "data_valid": [True] * 3,
        }, columns=KTRealtimeClient.KLINE_COLUMNS)
        kt_raw.attrs.update({"source": "tencent_gtimg", "adjustment": "qfq",
                             "returned_count": 3, "source_errors": []})
        with patch("data.kt_realtime.get_kt_client") as kt_client:
            kt_client.return_value.get_kline.return_value = kt_raw
            kt_frame = history._try_kt_daily("600519", "2026-09-20", "2026-09-24")
            assert kt_frame is not None and len(kt_frame) == 3, "KT日线必须映射为统一schema"
            assert list(kt_frame["date"]) == ["2026-09-22", "2026-09-23", "2026-09-24"], \
                "date必须取自data_date"
            assert kt_frame.attrs["source"] == "kt", "数据源必须标记为kt"
            get_kline_kwargs = kt_client.return_value.get_kline.call_args.kwargs
            assert get_kline_kwargs.get("period") == "d" and 1 <= get_kline_kwargs.get("count", 0) <= 1000, \
                "KT请求必须是日K且count在API上限内"
            kt_raw.attrs["adjustment"] = "unadjusted"
            assert history._try_kt_daily("600519", "2026-09-20", "2026-09-24") is None, \
                "无前复权数据必须回退已有源"
        assert history._try_kt_daily("600519", "2026-09-20", "2026-09-24", adjust="hfq") is None, \
            "后复权请求不得走腾讯前复权端点"

        # 瀑布接管：同花顺覆盖不足时，KT日线必须满足730天研究区间
        kt_full = pd.DataFrame({
            "date": list(pd.bdate_range("2026-03-02", "2026-09-24").strftime("%Y-%m-%d")),
            "open": 10, "high": 11, "low": 9, "close": 10,
        })
        with patch("data.history._try_cache", return_value=None), \
                patch("data.history._try_hithink", return_value=old_hithink), \
                patch("data.history._try_kt_daily", return_value=kt_full) as kt_step, \
                patch("data.history._try_longbridge", return_value=None) as longport_step, \
                patch("data.history._fetch_baostock", return_value=None) as baostock_step, \
                patch("data.history._save_to_cache") as save:
            covered_kt = get_daily("600519", "2026-03-02", "2026-09-24", require_full_range=True)
            assert covered_kt.attrs["source"] == "kt", \
                f"KT必须接管完整区间 (got {covered_kt.attrs['source']})"
            kt_step.assert_called_once()
            longport_step.assert_not_called()
            baostock_step.assert_not_called()
            save.assert_called_once()

        with patch("data.history._try_cache", return_value=None), \
                patch("data.history._try_hithink", return_value=None), \
                patch("data.history._try_kt_daily", return_value=None), \
                patch("data.history._try_longbridge", return_value=None), \
                patch("data.history._fetch_baostock", return_value=pd.DataFrame()):
            assert get_daily("600519", "2020-01-01", "2020-01-02").empty, \
                "全源无数据时保持空DataFrame兼容契约"
        print("历史K线缓存新鲜度测试通过")
    finally:
        database.DB_PATH = old_db_path
        for path in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    main()
