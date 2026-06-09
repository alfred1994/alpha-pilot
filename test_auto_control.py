#!/usr/bin/env python3
"""
自动盯盘暂停/恢复测试

验证暂停状态下盘中会跳过止损巡检、扫描和模拟执行，
Watchdog不会把暂停状态误判为停滞故障。
"""
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scheduler.auto_trader import AutoTraderState, run_auto_cycle
from scheduler.control import (
    get_auto_control_state,
    pause_auto_trader,
    resume_auto_trader,
)
from scheduler.watchdog import run_auto_watchdog


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def main():
    temp_paths = []
    try:
        control_file = tempfile.NamedTemporaryFile(suffix="_auto_control.json", delete=False)
        control_path = control_file.name
        control_file.close()
        os.unlink(control_path)
        temp_paths.append(control_path)

        state_file = tempfile.NamedTemporaryFile(suffix="_auto_state.json", delete=False)
        state_path = state_file.name
        state_file.close()
        temp_paths.append(state_path)

        db_file = tempfile.NamedTemporaryFile(suffix="_quant.db", delete=False)
        db_path = db_file.name
        db_file.close()
        os.unlink(db_path)
        temp_paths.append(db_path)

        paused = pause_auto_trader("人工观察行情", control_file=control_path)
        assert_true(paused["paused"], "暂停状态已写入")
        assert_true(get_auto_control_state(control_path)["reason"] == "人工观察行情", "暂停原因可读取")

        calls = []

        def fake_check_stops_once():
            calls.append("check_stops_once")
            return {"checked": 1, "sold": 0, "trades": []}

        def fake_run_scan():
            calls.append("run_scan")
            return SimpleNamespace(candidates=[1], decisions=[1])

        def fake_execute_trades():
            calls.append("execute_trades")
            return SimpleNamespace(executed_orders=[1], risk_triggered=[], errors=[])

        now = datetime(2026, 6, 9, 10, 0)
        state = AutoTraderState(date="2026-06-09")
        result = run_auto_cycle(
            state=state,
            force_scan=True,
            status_override="盘中",
            trading_day_override=True,
            today_override="2026-06-09",
            now_override=now,
            now_ts_override=1000,
            services={
                "check_stops_once": fake_check_stops_once,
                "run_scan": fake_run_scan,
                "execute_trades": fake_execute_trades,
            },
            persist_state=True,
            record_event=True,
            state_file=state_path,
            db_path=db_path,
            control_file=control_path,
            notify=False,
        )
        assert_true(calls == [], "暂停时不会调用止损/扫描/执行")
        assert_true(any("自动盯盘已暂停" in action for action in result["actions"]), "暂停动作写入自动循环报告")

        watchdog_items = run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            db_path=db_path,
            control_file=control_path,
            max_loop_lag_sec=300,
            max_scan_lag_sec=300,
            max_stop_lag_sec=120,
        )
        critical = [item.name for item in watchdog_items if item.severity == "critical"]
        assert_true(critical == [], "暂停状态下Watchdog不报critical")
        assert_true(any(item.name == "盘中交易动作" and item.ok for item in watchdog_items), "Watchdog识别盘中暂停")

        resumed = resume_auto_trader("恢复自动交易", control_file=control_path)
        assert_true(not resumed["paused"], "恢复状态已写入")

        print("自动盯盘暂停/恢复测试通过")

    finally:
        for path in temp_paths:
            for candidate in [path, f"{path}-wal", f"{path}-shm"]:
                if os.path.exists(candidate):
                    os.unlink(candidate)


if __name__ == "__main__":
    main()
