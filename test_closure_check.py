#!/usr/bin/env python3
"""
正式模拟盘日内闭环缺口诊断测试

验证闭环诊断能根据AI交易员日报行识别：
  - 盘中缺扫描
  - 扫描/执行后缺复盘
  - 完整闭环
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scheduler.closure_check as closure_check
from scheduler.closure_check import format_closure_check, run_closure_check


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _report(row, adaptive=True):
    return {
        "start_date": row["date"],
        "end_date": row["date"],
        "daily": [row],
        "adaptive": {"available": adaptive},
    }


def _row(**updates):
    row = {
        "date": "2026-06-09",
        "statuses": [],
        "cycle_count": 0,
        "scan_count": 0,
        "execute_count": 0,
        "trade_count": 0,
        "decision_count": 0,
        "review_count": 0,
        "lesson_count": 0,
        "diagnosis": "无循环",
        "closed_loop": False,
    }
    row.update(updates)
    return row


def main():
    original_next = closure_check.next_trading_day
    closure_check.next_trading_day = lambda date=None, n=1: "20260610"
    try:
        missing_scan = run_closure_check(
            date="2026-06-09",
            now=datetime(2026, 6, 9, 10, 0, 0),
            report=_report(_row(statuses=["盘中"], cycle_count=1)),
            status_override="盘中",
            trading_day_override=True,
        )
        text = format_closure_check(missing_scan)
        assert_true(missing_scan["overall"]["level"] == "critical", "盘中缺扫描为critical")
        assert_true("盘中扫描" in missing_scan["overall"]["critical"], "缺扫描进入critical列表")
        assert_true("--force-scan" in missing_scan["next_action"]["command"], "缺扫描建议强制观察扫描")
        assert_true("正式模拟盘日内闭环缺口" in text, "格式化输出包含标题")

        missing_review = run_closure_check(
            date="2026-06-09",
            now=datetime(2026, 6, 9, 15, 10, 0),
            report=_report(_row(
                statuses=["盘中", "盘后"],
                cycle_count=3,
                scan_count=1,
                execute_count=1,
                trade_count=1,
                decision_count=1,
                diagnosis="缺复盘",
            )),
            status_override="盘后",
            trading_day_override=True,
        )
        assert_true(missing_review["overall"]["level"] == "critical", "盘后缺复盘为critical")
        assert_true("盘后复盘" in missing_review["overall"]["critical"], "缺复盘进入critical列表")
        assert_true("--review" in missing_review["next_action"]["command"], "缺复盘建议复盘命令")

        review_only = run_closure_check(
            date="2026-06-09",
            now=datetime(2026, 6, 9, 15, 20, 0),
            report=_report(_row(
                statuses=["盘后"],
                cycle_count=1,
                review_count=1,
                diagnosis="仅复盘",
            )),
            status_override="盘后",
            trading_day_override=True,
        )
        assert_true(review_only["overall"]["level"] == "warn", "仅复盘无交易样本时不是critical")
        assert_true("--force-scan" in review_only["next_action"]["command"], "仅复盘优先建议补盘中观察")

        closed = run_closure_check(
            date="2026-06-09",
            now=datetime(2026, 6, 9, 15, 20, 0),
            report=_report(_row(
                statuses=["盘前", "盘中", "盘后"],
                cycle_count=5,
                scan_count=1,
                execute_count=1,
                trade_count=1,
                decision_count=1,
                review_count=1,
                lesson_count=1,
                diagnosis="闭环",
                closed_loop=True,
            )),
            status_override="盘后",
            trading_day_override=True,
        )
        assert_true(closed["overall"]["level"] == "ok", "完整闭环为ok")
        assert_true(closed["overall"]["closed_loop"], "完整闭环标记为True")
        assert_true(closed["next_action"]["severity"] == "ok", "完整闭环只建议继续观察")

        holiday = run_closure_check(
            date="2026-06-13",
            now=datetime(2026, 6, 13, 10, 0, 0),
            report=_report(_row(date="2026-06-13")),
            status_override="休市",
            trading_day_override=False,
        )
        assert_true(holiday["overall"]["level"] == "ok", "休市无需闭环")

        print("正式模拟盘日内闭环缺口诊断测试通过")
    finally:
        closure_check.next_trading_day = original_next


if __name__ == "__main__":
    main()
