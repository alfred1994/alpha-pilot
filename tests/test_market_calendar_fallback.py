#!/usr/bin/env python3
"""交易日历降级链路回归测试。

Baostock 不可用时：表内年份用本地节假日表剔除工作日休市，
表外年份退回"周一至五全是交易日"。Baostock 可用时行为不变。
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data.history as history_module
import scheduler.market_calendar as mc


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _reset_cache():
    mc._trading_dates_cache = None
    mc._cache_date = None


def test_holiday_table_fallback():
    print("测试1: Baostock失败 → 节假日表降级(2025)")
    _reset_cache()
    with mock.patch.object(
        history_module, "query_baostock_trade_dates",
        side_effect=RuntimeError("baostock down"),
    ):
        assert_true(not mc.is_trading_day("20251001"), "国庆节(10/1)不是交易日")
        assert_true(not mc.is_trading_day("20251006"), "国庆+中秋调休(10/6)不是交易日")
        assert_true(mc.is_trading_day("20251009"), "10/9是交易日")
        assert_true(not mc.is_trading_day("20260101"), "2026元旦不是交易日")
        assert_true(not mc.is_trading_day("20260217"), "2026春节(2/17)不是交易日")
        assert_true(mc.is_trading_day("20260223"), "2026春节后首个周一(2/23)是交易日")
        assert_true(mc.next_trading_day("20250127") == "20250205",
                    f"春节跨越: 1/27的下一交易日是2/5 (got {mc.next_trading_day('20250127')})")
        assert_true(mc.prev_trading_day("20250101") == "20241231",
                    "元旦前上一交易日为2024/12/31")


def test_unknown_year_falls_back_to_weekdays():
    print("测试2: 表外年份 → 工作日兜底")
    _reset_cache()
    with mock.patch.object(
        history_module, "query_baostock_trade_dates",
        side_effect=RuntimeError("baostock down"),
    ):
        # 2031 不在节假日表：工作日视为交易日（劳动节会被误判，属诚实降级）
        assert_true(mc.is_trading_day("20310501"), "表外年份工作日视为交易日")
        assert_true(not mc.is_trading_day("20310503"), "表外年份周末仍非交易日")


def test_baostock_path_unaffected():
    print("测试3: Baostock正常时行为不变")
    _reset_cache()
    fake_rows = {
        "data": [
            ["2026-09-15", "1"],
            ["2026-09-16", "1"],
            ["2026-09-20", "0"],  # 周日
        ],
    }
    with mock.patch.object(
        history_module, "query_baostock_trade_dates", return_value=fake_rows
    ):
        assert_true(mc.is_trading_day("20260915"), "Baostock标记为交易日")
        assert_true(not mc.is_trading_day("20260920"), "Baostock标记为休市(周日)")
        assert_true(not mc.is_trading_day("20260921"),
                    "Baostock范围内未列出的日期视为非交易日")


def main():
    try:
        test_holiday_table_fallback()
        test_unknown_year_falls_back_to_weekdays()
        test_baostock_path_unaffected()
    finally:
        _reset_cache()
    print("\n全部交易日历降级测试通过")


if __name__ == "__main__":
    main()
