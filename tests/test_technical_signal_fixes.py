#!/usr/bin/env python3
"""技术信号修复回归测试。

覆盖三类修复:
1. board_limit_pct: 涨跌停阈值按板块自适应(主板10%/创业科创20%/北交30%/ST 5%)。
2. zt_reversal / limit_down_reversal / uptrend_limit_down 在创业板 20cm 股上能识别涨跌停。
3. Wyckoff Spring: 区间高/低点排除检测窗口后, 探测日可触发(修复前恒不可达)。
4. 一目均衡表: 价格 vs 云判定使用当前云位(shift 后的 iloc[-1]), 而非 26 天前的云。
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.strategies.base import board_limit_pct
from strategy.strategies.zt_reversal import ZTReversalStrategy
from strategy.strategies.limit_down_reversal import LimitDownReversalStrategy
from signals.technical import wyckoff_signal, ichimoku_signal


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _board_ok(code, expected, name=""):
    actual = board_limit_pct(code, name)
    assert_true(abs(actual - expected) < 1e-9, f"{code} 涨跌停幅度={actual} (期望{expected})")


def test_board_limit_pct():
    print("测试1: 板块涨跌停幅度")
    _board_ok("600519", 0.10)
    _board_ok("000001", 0.10)
    _board_ok("002384", 0.10)
    _board_ok("300308", 0.20)
    _board_ok("301001", 0.20)
    _board_ok("688001", 0.20)
    _board_ok("920001", 0.30)
    _board_ok("sh600519", 0.10)
    _board_ok("600519", 0.05, name="ST某某")


def _make_ohlcv(closes, volumes=None):
    closes = list(closes)
    n = len(closes)
    if volumes is None:
        volumes = [1_000_000] * n
    opens = [c for c in closes]
    highs = [max(o, c) * 1.001 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.999 for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D").strftime("%Y-%m-%d"),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def test_zt_reversal_20cm_board():
    print("测试2: 涨停洗盘策略识别创业板20cm涨停")
    closes = [10.0] * 70
    closes[-1] = 12.0  # 非信号日
    df = _make_ohlcv(closes)
    # 构造: 昨日+19.9%涨停(收盘=最高), 今日十字星
    df.loc[68, "close"] = 10.0
    df.loc[69, "open"] = 10.0
    df.loc[69, "close"] = 11.99
    df.loc[69, "high"] = 11.99
    df.loc[69, "low"] = 10.0
    df.loc[70, "open"] = 11.99
    df.loc[70, "close"] = 11.97
    df.loc[70, "high"] = 12.05
    df.loc[70, "low"] = 11.90
    # 量能温和爬升, 满足短期均量上穿长期均量且不超2倍
    for i, idx in enumerate(range(60, 70)):
        df.loc[idx, "volume"] = 1_000_000 + (i + 1) * 50_000
    df.loc[70, "volume"] = 1_100_000

    signal = ZTReversalStrategy().generate_signals("300308", df)
    assert_true(signal.action == "BUY", f"创业板+19.9%识别为涨停并触发信号({signal.action}: {signal.reason})")


def test_limit_down_reversal_20cm_board():
    print("测试3: 跌停反转策略识别创业板20cm跌停")
    # 连续两日 -19.6% 跌停, 今日缩量回升+2.5%
    c1, c2, c3, c4 = 20.0, 16.08, 12.93, 13.25
    closes = [20.0] * 40 + [c2, c3, c4]
    df = _make_ohlcv(closes)
    df.loc[len(df) - 2, "volume"] = 500_000  # 止跌日缩量

    signal = LimitDownReversalStrategy().generate_signals("300308", df)
    assert_true(
        signal.action == "BUY",
        f"创业板-19.5%识别为跌停并触发反转信号({signal.action}: {signal.reason})",
    )


def test_wyckoff_spring_trigger():
    print("测试4: Wyckoff Spring 可触发")
    n = 90
    closes = []
    for i in range(n):
        closes.append(10.4 if i % 2 == 0 else 10.1)
    df = _make_ohlcv(closes)
    # 前70根构成区间 [9.9, 10.5] (high/low 由 _make_ohlcv 生成: ±0.1%)
    # 检测窗口内某日向下假突破: low=9.5 < 区间低点, close=10.2 收回
    spring_idx = 80
    df.loc[spring_idx, "low"] = 9.5
    df.loc[spring_idx, "close"] = 10.2
    df.loc[spring_idx, "open"] = 10.2
    df.loc[spring_idx, "high"] = 10.3

    result = wyckoff_signal(df)
    assert_true(
        "Spring" in result.detail,
        f"向下假突破被识别为 Spring ({result.detail})",
    )
    assert_true(result.score >= 75, f"Spring 信号得分 {result.score} >= 75")


def test_ichimoku_uses_current_cloud():
    print("测试5: 一目均衡表使用当前云位")
    n = 90
    closes = [10.0] * 50
    # 50-75 上涨到30, 76-89 回落到13
    for i in range(26):
        closes.append(10.0 + (30.0 - 10.0) * (i + 1) / 26)
    for i in range(14):
        closes.append(30.0 + (13.0 - 30.0) * (i + 1) / 14)
    df = _make_ohlcv(closes)

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(26)
    cur_cloud = max(float(senkou_a.iloc[-1]), float(((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26).iloc[-1]))
    old_cloud = max(
        float(senkou_a.iloc[-26]),
        float(((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26).iloc[-26]),
    )
    price = float(close.iloc[-1])
    assert_true(price < cur_cloud, f"测试前置: 现价{price:.2f}低于当前云{cur_cloud:.2f}")
    assert_true(price > old_cloud, f"测试前置: 现价{price:.2f}高于26天前的云{old_cloud:.2f}")

    result = ichimoku_signal(df)
    assert_true(
        "价格在云下" in result.detail,
        f"按当前云位判定为云下 ({result.detail})",
    )


def main():
    test_board_limit_pct()
    test_zt_reversal_20cm_board()
    test_limit_down_reversal_20cm_board()
    test_wyckoff_spring_trigger()
    test_ichimoku_uses_current_cloud()
    print("\n全部技术信号修复测试通过")


if __name__ == "__main__":
    main()
