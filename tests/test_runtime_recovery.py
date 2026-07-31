#!/usr/bin/env python3
"""盘中超时降级和低位选股回归测试。"""
import json
import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.snapshot import with_timeout
from scheduler import pipeline
from strategy.low_position_picker import _get_early_stage_candidates


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def test_timeout_returns_without_waiting_for_worker():
    release = threading.Event()
    started = time.monotonic()
    result = with_timeout(
        lambda: release.wait(2),
        timeout=0.05,
        fallback="fallback",
        desc="测试慢调用",
    )
    elapsed = time.monotonic() - started
    release.set()
    assert_true(result == "fallback", "超时调用返回降级结果")
    assert_true(elapsed < 0.5, "超时后不会等待后台线程结束")


def test_early_scan_persists_fresh_plan():
    fd, path = tempfile.mkstemp(suffix="_signal_cache.json")
    os.close(fd)
    os.unlink(path)
    try:
        with patch.object(pipeline, "SIGNAL_CACHE_FILE", path):
            plan = pipeline.fast_scan(
                budget_seconds=0,
                candidate_items=[{"code": "600519", "name": "测试股票"}],
            )
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        assert_true(plan.elapsed >= 0, "时间不足时返回本轮计划")
        assert_true(saved["date"] == plan.date, "时间不足时仍保存本轮TradePlan")
        assert_true(saved["orders"] == [], "时间不足的计划不会复用旧订单")
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_low_price_picker_uses_project_realtime_api():
    active = {"000001": "测试股份", "600000": "测试银行"}
    quotes = [
        SimpleNamespace(code="000001", price=9.5, change_pct=1.2),
        SimpleNamespace(code="600000", price=16.0, change_pct=1.0),
    ]
    with patch("strategy.stock_picker._get_active_stocks", return_value=active), patch(
        "data.realtime.get_realtime_batch", return_value=quotes
    ):
        candidates = _get_early_stage_candidates()
    assert_true(list(candidates) == ["000001"], "低价股选股使用批量实时行情并保留合格候选")


def main():
    test_timeout_returns_without_waiting_for_worker()
    test_early_scan_persists_fresh_plan()
    test_low_price_picker_uses_project_realtime_api()
    print("盘中运行恢复测试通过")


if __name__ == "__main__":
    main()
