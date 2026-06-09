#!/usr/bin/env python3
"""
健康检查自动盯盘控制状态测试

验证暂停状态会作为可选WARN出现在健康检查中，
但不会成为必需失败项。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scheduler.control as control
from scheduler.health import format_health_report, run_health_check


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    original = control.get_auto_control_state
    try:
        control.get_auto_control_state = lambda: {
            "paused": True,
            "reason": "健康检查测试暂停",
            "updated_at": "2026-06-09T10:00:00",
            "updated_by": "test",
        }
        items = run_health_check()
        control_items = [item for item in items if item.name == "自动盯盘控制"]
        assert_true(len(control_items) == 1, "健康检查包含自动盯盘控制状态")
        assert_true(not control_items[0].ok, "暂停状态会标记为非OK")
        assert_true(not control_items[0].required, "暂停状态是可选告警")
        report = format_health_report(items)
        assert_true("[WARN] 自动盯盘控制" in report, "健康报告显示控制状态WARN")

        print("健康检查自动盯盘控制状态测试通过")
    finally:
        control.get_auto_control_state = original


if __name__ == "__main__":
    main()
