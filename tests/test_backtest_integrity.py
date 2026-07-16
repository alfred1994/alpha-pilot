#!/usr/bin/env python3
"""回测费用、T+1 与下一交易日成交回归测试。"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portfolio.backtest import BacktestEngine
from portfolio.position import PositionManager
from signals import Direction
from signals.composite import CompositeSignal


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def signal(code, direction):
    return CompositeSignal(
        code=code,
        name="测试股票",
        final_score=80,
        direction=direction,
        confidence=0.8,
        signals={},
        reason="回归测试",
    )


def test_position_manager_charges_fees_and_enforces_t_plus_one():
    manager = PositionManager(initial_capital=1005, max_single_pct=1.0)
    trade = manager.buy("600519", "贵州茅台", 10, "2026-01-02", amount=1000)
    assert_true(trade is not None and manager.cash == 0, "买入金额和最低佣金同时从现金扣除")
    assert_true(manager.sell("600519", 10, "2026-01-02") is None, "买入当日受T+1限制不可卖出")
    sell = manager.sell("600519", 10, "2026-01-03")
    assert_true(sell is not None, "下一交易日可以卖出")
    assert_true(abs(manager.cash - 994.5) < 0.000001, "卖出现金扣除佣金和万五印花税")
    assert_true(sell.commission == 5 and sell.stamp_tax == 0.5, "卖出交易记录保留完整费用")


def test_backtest_uses_previous_bar_signal_and_next_bar_execution():
    dates = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
    price_data = pd.DataFrame([
        {"date": date, "code": "600519", "close": 10.0}
        for date in dates
    ])
    observed_signal_dates = []

    def signals_func(signal_date):
        observed_signal_dates.append(signal_date)
        direction = Direction.BUY if signal_date == "2026-01-01" else Direction.SELL
        return [signal("600519", direction)]

    result = BacktestEngine(initial_capital=10_000).run(
        signals_func=signals_func,
        price_data=price_data,
        start_date=dates[0],
        end_date=dates[-1],
        signal_interval=1,
    )
    buy_trade = next(item for item in result.trades if item.action == "买入")
    sell_trade = next(item for item in result.trades if item.action == "卖出")
    assert_true(observed_signal_dates[0] == "2026-01-01", "第二个交易日使用首日收盘信号")
    assert_true(buy_trade.date == "2026-01-02", "买入在下一交易日执行")
    assert_true(sell_trade.date == "2026-01-03", "卖出在下一交易日执行并满足T+1")
    assert_true(result.final_assets < 10_000, "平价往返收益准确反映交易费用")


if __name__ == "__main__":
    test_position_manager_charges_fees_and_enforces_t_plus_one()
    test_backtest_uses_previous_bar_signal_and_next_bar_execution()
    print("回测完整性测试通过")
