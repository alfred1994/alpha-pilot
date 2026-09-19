#!/usr/bin/env python3
"""Vibe-Trading 本地日线面板构建测试（完全离线：数据获取全部打桩）。"""
import importlib
import json
import os
import sys
import tempfile
from datetime import datetime
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data.vibe_panel as vibe_panel
from data.vibe_panel import (
    build_panel_long,
    export_panel_csv,
    period_bounds,
    resolve_universe_codes,
)


def ok(message):
    print(f"  OK {message}")


def _daily_frame(days, base_price=10.0, code="600519", with_code_column=False):
    rows = []
    price = base_price
    for index in range(days):
        day = pd.Timestamp("2024-01-01") + pd.Timedelta(days=index)
        rows.append({
            "date": day.strftime("%Y%m%d"),
            "open": round(price, 2), "high": round(price * 1.02, 2),
            "low": round(price * 0.98, 2), "close": round(price, 2),
            "volume": 1_000_000 + index, "amount": round(price * (1_000_000 + index), 2),
            "turn": 1.0, "pctChg": 0.5,
        })
        price *= 1.01
    frame = pd.DataFrame(rows)
    if with_code_column:
        # k_daily SELECT * / baostock 源返回会自带 code 列
        frame["code"] = code
    return frame


def test_period_bounds():
    assert period_bounds("2024-2026") == ("2024-01-01", "2026-12-31")
    assert period_bounds("2024-03-01/2024-06-30") == ("2024-03-01", "2024-06-30")
    for bad in ("2024", "abc", "2026-2024"):
        try:
            period_bounds(bad)
            raise AssertionError(f"{bad} 应被拒绝")
        except ValueError:
            pass
    ok("period 解析与倒置校验")


def test_resolve_universe_codes():
    with tempfile.TemporaryDirectory() as tmp:
        pool_path = os.path.join(tmp, "research_universe.json")
        with open(pool_path, "w", encoding="utf-8") as fh:
            json.dump({"codes": ["600519", "000001", "BAD", "300750", None, "688111",
                                 {"code": "601318", "name": "中国平安"}]}, fh)
        with mock.patch.object(vibe_panel, "UNIVERSE_FILE", pool_path):
            codes = resolve_universe_codes("pool", pool_limit=2)
            assert codes == ["600519", "000001"]
            codes = resolve_universe_codes("pool", pool_limit=0)
            # 只按 6 位数字格式过滤；市场板块资格由研究池刷新时保证
            assert codes == ["600519", "000001", "300750", "688111", "601318"]
        empty_pool = os.path.join(tmp, "empty.json")
        with open(empty_pool, "w", encoding="utf-8") as fh:
            json.dump({"codes": []}, fh)
        with mock.patch.object(vibe_panel, "UNIVERSE_FILE", empty_pool):
            try:
                resolve_universe_codes("pool")
                raise AssertionError("空代码池应报错")
            except ValueError:
                pass
        ok("研究池解析：截断、非法代码过滤、空池报错")

        codes_path = os.path.join(tmp, "codes.txt")
        with open(codes_path, "w", encoding="utf-8") as fh:
            fh.write("# 手工维护\n600519\n\n000001  # 行内注释\nbadline\n")
        assert resolve_universe_codes(f"file:{codes_path}") == ["600519", "000001"]
        try:
            resolve_universe_codes("file:/no/such/file.txt")
            raise AssertionError("缺失文件应报错")
        except ValueError:
            pass
        ok("代码文件解析：注释与空行被忽略")


def test_build_panel_long():
    with tempfile.TemporaryDirectory() as tmp:
        long_days = _daily_frame(120)
        short_days = _daily_frame(10, code="000001")

        def fake_get_daily(code, start_date=None, end_date=None, adjust="qfq",
                           simple=True, require_full_range=False):
            assert require_full_range is False, "面板取数必须容忍尾部陈旧缓存"
            if code == "600519":
                # 自带 code 列，模拟 k_daily 缓存命中（曾触发 pandas insert 撞列）
                return _daily_frame(120, with_code_column=True)
            if code == "000001":
                return short_days.copy()
            if code == "300750":
                raise RuntimeError("source down")
            return None

        with mock.patch.object(vibe_panel.data_history, "get_daily", side_effect=fake_get_daily):
            long_df, stats = build_panel_long(
                ["600519", "000001", "300750", "600036"], "2024-01-01/2024-06-30",
            )
        assert stats["used"] == 1 and stats["requested"] == 4
        assert stats["skipped"]["000001"].startswith("rows<")
        assert stats["skipped"]["300750"] == "error:RuntimeError"
        assert stats["skipped"]["600036"] == "empty"
        assert list(long_df.columns) == vibe_panel.PANEL_COLUMNS
        assert set(long_df["code"]) == {"600519"}
        dates = long_df["date"].unique()
        assert dates[0] == "2024-01-01" and len(dates) == 120
        first = long_df.iloc[0]
        assert abs(first["vwap"] - first["amount"] / first["volume"]) < 1e-6
        ok("面板构建：日期归一化、vwap、自带 code 列不撞列、覆盖不足/失败代码跳过")

        # 未来区间收敛到今天，避免全量缓存过期触发外部源重试链
        with mock.patch.object(vibe_panel.data_history, "get_daily",
                               return_value=_daily_frame(120)):
            _, clamp_stats = build_panel_long(["600519"], "2024-2030")
        assert clamp_stats["end_date"] == datetime.now().strftime("%Y-%m-%d")
        ok("period 终点收敛到今天")

        with mock.patch.object(vibe_panel.data_history, "get_daily", return_value=None):
            empty_df, empty_stats = build_panel_long(["600519"], "2024-2026")
        assert empty_df.empty and empty_stats["used"] == 0
        try:
            with mock.patch.object(vibe_panel.data_history, "get_daily", return_value=None):
                export_panel_csv(["600519"], "2024-2026", out_dir=os.path.join(tmp, "p"))
            raise AssertionError("空面板导出应报错")
        except ValueError:
            pass
        ok("全空面板不落盘并显式报错")

        with mock.patch.object(vibe_panel.data_history, "get_daily",
                               return_value=_daily_frame(120)):
            path, stats = export_panel_csv(["600519"], "2024-2026",
                                           out_dir=os.path.join(tmp, "p"))
        expected_end = min("2026-12-31", datetime.now().strftime("%Y-%m-%d"))
        assert os.path.basename(path) == f"panel_2024-01-01_{expected_end}.csv"
        stored = pd.read_csv(path, dtype={"code": str})
        assert stored.iloc[0]["code"] == "600519"  # 前导零不被吃掉
        assert stats["rows"] == 120
        ok("面板 CSV 落盘且代码按字符串保留")


def test_driver_panel_pivot():
    driver_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "vibe", "vibe_tool_driver.py",
    )
    spec = importlib.util.spec_from_file_location("vibe_tool_driver_pivot", driver_path)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)

    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(vibe_panel.data_history, "get_daily",
                               return_value=_daily_frame(80)):
            csv_path, _ = export_panel_csv(["600519", "000001"], "2024-01-01/2024-04-30",
                                           out_dir=tmp)
        panel = driver._panel_from_long_csv(csv_path, period="2024-01-01/2024-02-29")
        for key in ("open", "high", "low", "close", "volume", "amount", "vwap"):
            assert key in panel, key
        close = panel["close"]
        assert list(close.columns) == ["000001", "600519"]  # 字符串列名
        assert close.index.is_monotonic_increasing
        assert (close.index <= pd.Timestamp("2024-02-29")).all()
        assert not close.empty
        try:
            driver._panel_from_long_csv(csv_path, period="2030-2031")
            raise AssertionError("period 外无数据应报错")
        except RuntimeError:
            pass
        ok("driver 侧长表→宽表 pivot 正确且受 period 约束")


def main():
    print("== Vibe-Trading 本地面板测试 ==")
    test_period_bounds()
    test_resolve_universe_codes()
    test_build_panel_long()
    test_driver_panel_pivot()
    print("全部通过")


if __name__ == "__main__":
    main()
