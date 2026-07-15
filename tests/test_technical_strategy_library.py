#!/usr/bin/env python3
"""技术策略库注册、K线输入和组合投票回归测试。"""
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.technical import all_technical_signals
from portfolio.backtest import SimpleBacktestEngine
from strategy.strategies import get_strategy, list_strategies
from strategy.strategies.base import Signal


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def make_kline(rows=120):
    dates = pd.date_range("2026-01-01", periods=rows, freq="B")
    base = 10 + np.sin(np.linspace(0, 8 * np.pi, rows)) * 0.35 + np.linspace(0, 1.2, rows)
    close = pd.Series(base)
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": close * 0.995,
        "high": close * 1.015,
        "low": close * 0.985,
        "close": close,
        "volume": 1_000_000 + (np.arange(rows) % 9) * 20_000,
    })


def test_new_strategies_are_registered_and_return_contract_signal():
    data = make_kline()
    available = {item["name"] for item in list_strategies()}
    expected = {"macd_trend", "bollinger_squeeze", "kdj_reversal", "technical_ensemble"}
    assert_true(expected.issubset(available), "新增技术策略已注册")

    for name in sorted(expected):
        signal = get_strategy(name).generate_signals("600519", data)
        assert_true(signal.action in {"BUY", "SELL", "HOLD"}, f"{name} 返回标准动作")
        assert_true(signal.date == data["date"].iloc[-1], f"{name} 返回最新K线日期")
        assert_true(math.isfinite(signal.score), f"{name} 信号分数为有限数")


def test_bollinger_squeeze_detects_volume_confirmed_breakout():
    data = make_kline(100)
    data.loc[:98, "open"] = 10.0
    data.loc[:98, "high"] = 10.05
    data.loc[:98, "low"] = 9.95
    data.loc[:98, "close"] = 10.0
    data.loc[:98, "volume"] = 1_000_000
    data.loc[99, ["open", "high", "low", "close", "volume"]] = [10.1, 11.2, 10.0, 11.0, 2_000_000]

    signal = get_strategy("bollinger_squeeze").generate_signals("600519", data)
    assert_true(signal.action == "BUY", "布林收敛后放量上轨突破生成买入信号")
    assert_true(signal.metadata["upper"] > 0, "布林策略输出可解释指标")


def test_technical_dimension_includes_basic_kline_indicators():
    signals = all_technical_signals(make_kline(150))
    names = {item.name for item in signals}
    assert_true({"均线交叉", "MACD", "量价背离"}.issubset(names), "技术维度纳入均线、MACD和量价信号")


def test_strategy_profiles_and_regime_aware_ensemble_threshold():
    profiles = {item["name"]: item for item in list_strategies()}
    assert_true(profiles["macd_trend"]["category"] == "趋势跟踪", "策略列表提供策略类别")
    assert_true("bull" in profiles["macd_trend"]["regimes"], "策略列表提供适用市场环境")

    bull_data = make_kline()
    bull_data["close"] = np.linspace(10, 15, len(bull_data))
    bear_data = make_kline()
    bear_data["close"] = np.linspace(15, 10, len(bear_data))
    assert_true(SimpleBacktestEngine._infer_technical_regime(bull_data) == "bull", "上升均线结构识别为牛市")
    assert_true(SimpleBacktestEngine._infer_technical_regime(bear_data) == "bear", "下降均线结构识别为熊市")

    class AlwaysBuy:
        name = "测试买入策略"

        @staticmethod
        def generate_signals(code, df, **kwargs):
            return Signal(date=df["date"].iloc[-1], code=code, action="BUY", score=80, reason="测试买入")

    ensemble = get_strategy("technical_ensemble")
    ensemble._components = [(AlwaysBuy(), "trend"), (AlwaysBuy(), "breakout")]
    sideways = ensemble.generate_signals("600519", make_kline(), market_regime="sideways")
    bear = ensemble.generate_signals("600519", make_kline(), market_regime="bear")
    assert_true(sideways.action == "BUY", "震荡环境两票买入共识可以触发")
    assert_true(bear.action == "HOLD", "熊市环境两票买入不足以触发")
    assert_true(bear.metadata["required_buy_votes"] == 3, "熊市环境提高买入共识门槛")


if __name__ == "__main__":
    test_new_strategies_are_registered_and_return_contract_signal()
    test_bollinger_squeeze_detects_volume_confirmed_breakout()
    test_technical_dimension_includes_basic_kline_indicators()
    test_strategy_profiles_and_regime_aware_ensemble_threshold()
    print("技术策略库测试通过")
