#!/usr/bin/env python3
"""交易链路超时预算契约测试。"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    AUTO_RESCUE_SCAN_BUDGET_SECONDS,
    BAOSTOCK_TIMEOUT,
    FAST_SCAN_BUDGET_SECONDS,
    FAST_SCAN_LLM_DECISION_TIMEOUT,
    FAST_SCAN_LLM_RESULT_TIMEOUT,
    FAST_SCAN_SELL_LLM_TIMEOUT,
    FAST_SCAN_STOCK_PICK_TIMEOUT,
    MIMO_CONNECT_TIMEOUT,
    MIMO_HTTP_TIMEOUT,
)
from data.history import BAOSTOCK_TIMEOUT as HISTORY_BAOSTOCK_TIMEOUT
from scheduler.auto_trader import _run_rescue_scan_default
from scheduler.pipeline import fast_scan, run_scan
from strategy.mimo_client import DEFAULT_CONNECT_TIMEOUT, DEFAULT_HTTP_TIMEOUT


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    assert_true(MIMO_HTTP_TIMEOUT >= 90, "MiMo 单次 HTTP 超时不少于90秒")
    assert_true(MIMO_CONNECT_TIMEOUT >= 15, "MiMo 连接超时不少于15秒")
    assert_true(BAOSTOCK_TIMEOUT >= 75, "Baostock 超时不少于75秒")
    assert_true(FAST_SCAN_BUDGET_SECONDS >= 480, "常规扫描总预算不少于480秒")
    assert_true(FAST_SCAN_STOCK_PICK_TIMEOUT >= 90, "选股超时不少于90秒")
    assert_true(FAST_SCAN_LLM_DECISION_TIMEOUT >= 90, "买入决策超时不少于90秒")
    assert_true(FAST_SCAN_SELL_LLM_TIMEOUT >= 90, "持仓卖出决策超时不少于90秒")
    assert_true(FAST_SCAN_LLM_RESULT_TIMEOUT >= 150, "并发决策汇总等待不少于150秒")
    assert_true(AUTO_RESCUE_SCAN_BUDGET_SECONDS >= 360, "救援扫描总预算不少于360秒")
    assert_true(DEFAULT_HTTP_TIMEOUT == MIMO_HTTP_TIMEOUT, "MiMo 客户端使用统一HTTP预算")
    assert_true(DEFAULT_CONNECT_TIMEOUT == MIMO_CONNECT_TIMEOUT, "MiMo 客户端使用统一连接预算")
    assert_true(HISTORY_BAOSTOCK_TIMEOUT == BAOSTOCK_TIMEOUT, "Baostock 使用统一预算")
    assert_true(
        inspect.signature(fast_scan).parameters["budget_seconds"].default == FAST_SCAN_BUDGET_SECONDS,
        "快链路默认使用统一总预算",
    )
    assert_true(
        inspect.signature(run_scan).parameters["budget_seconds"].default == FAST_SCAN_BUDGET_SECONDS,
        "兼容扫描入口使用统一总预算",
    )
    rescue_source = inspect.getsource(_run_rescue_scan_default)
    assert_true(
        "budget_seconds=AUTO_RESCUE_SCAN_BUDGET_SECONDS" in rescue_source,
        "救援扫描使用统一总预算",
    )
    print("交易链路超时预算测试通过")


if __name__ == "__main__":
    main()
