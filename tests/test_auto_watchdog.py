#!/usr/bin/env python3
"""
自动盯盘Watchdog测试

使用临时状态文件和SQLite，验证运行态检查能识别：
  - 盘中正常运行
  - 盘中停滞
  - 盘后漏复盘
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from scheduler.auto_trader import AUTO_STAGE_BUDGET_SECONDS
from scheduler.watchdog import run_auto_watchdog


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _write_state(path: str, state: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _write_lock(path: str, payload: dict, mtime: float = None):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _by_name(items):
    return {item.name: item for item in items}


def _has_critical(items) -> bool:
    return any(item.severity == "critical" for item in items)


def main():
    temp_paths = []
    try:
        state_file = tempfile.NamedTemporaryFile(suffix="_auto_state.json", delete=False)
        state_path = state_file.name
        state_file.close()
        temp_paths.append(state_path)

        lock_file = tempfile.NamedTemporaryFile(suffix="_auto.lock", delete=False)
        lock_path = lock_file.name
        lock_file.close()
        temp_paths.append(lock_path)

        db_file = tempfile.NamedTemporaryFile(suffix="_quant.db", delete=False)
        db_path = db_file.name
        db_file.close()
        os.unlink(db_path)
        temp_paths.append(db_path)

        now = datetime(2026, 6, 9, 10, 5, 0)
        now_ts = now.timestamp()
        fresh_state = {
            "date": "2026-06-09",
            "last_prefetch_date": "2026-06-09",
            "last_regime_date": "2026-06-09",
            "last_scan_at": now_ts - 120,
            "last_execute_at": now_ts - 110,
            "last_stop_check_at": now_ts - 30,
            "last_review_date": "",
            "loop_count": 4,
            "last_status": "盘中",
            "last_error": "",
            "updated_at": "2026-06-09T10:04:30",
        }
        _write_state(state_path, fresh_state)
        _write_lock(lock_path, {
            "pid": 12345,
            "token": "watchdog-test",
            "updated_at": "2026-06-09T10:04:40",
        })
        with Database(db_path=db_path) as db:
            db.insert_auto_event({
                "date": "2026-06-09",
                "event_type": "auto_doctor",
                "status": "盘中",
                "actions": ["发现critical: 今日异常事件"],
                "error": "今日异常事件",
                "created_at": "2026-06-09T10:05:00",
            })
        self_error_items = run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
        )
        assert_true(
            _by_name(self_error_items)["今日异常事件"].severity != "critical",
            "Doctor自身诊断失败不触发今日异常critical",
        )

        with Database(db_path=db_path) as db:
            db.insert_auto_event({
                "date": "2026-06-09",
                "status": "盘中",
                "actions": ["盘中扫描: 候选3只 决策2条", "模拟执行: 成交1笔 风控0项 错误0项"],
                "created_at": "2026-06-09T10:04:00",
            })

        items = run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
            max_scan_lag_sec=300,
            max_stop_lag_sec=120,
        )
        named = _by_name(items)
        assert_true(not _has_critical(items), "盘中正常状态不会触发critical")
        assert_true(named["自动盘锁"].ok, "自动盘锁新鲜度正常")
        assert_true(named["盘中扫描"].ok, "盘中扫描新鲜度正常")
        assert_true(named["盘中模拟执行"].ok, "盘中模拟执行新鲜度正常")
        assert_true(named["盘中止损巡检"].ok, "盘中止损巡检新鲜度正常")

        browser_items = run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
            browser_process_probe=lambda: {
                "available": True,
                "instance_count": 3,
                "process_count": 12,
                "oldest_age_seconds": 8000,
            },
        )
        browser_item = _by_name(browser_items)["无头浏览器"]
        assert_true(browser_item.severity == "critical", "浏览器数量超过上限只触发只读critical")
        assert_true("不会误杀进程" in browser_item.suggestion, "浏览器Watchdog明确不终止进程")

        heartbeat_only_state = dict(fresh_state)
        heartbeat_only_state.update({
            "last_scan_at": now_ts - 2000,
            "last_execute_at": now_ts - 2000,
            "last_stop_check_at": now_ts - 500,
            "updated_at": "2026-06-09T10:04:50",
        })
        _write_state(state_path, heartbeat_only_state)
        heartbeat_only_items = run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
            max_loop_lag_sec=600,
            max_scan_lag_sec=300,
            max_stop_lag_sec=120,
        )
        heartbeat_only_named = _by_name(heartbeat_only_items)
        assert_true(heartbeat_only_named["自动循环新鲜度"].ok, "通用循环心跳仍可证明进程存活")
        assert_true(
            heartbeat_only_named["盘中扫描"].severity == "critical",
            "没有活跃阶段时通用心跳不能豁免过期扫描",
        )
        assert_true(
            heartbeat_only_named["盘中模拟执行"].severity == "critical",
            "没有活跃阶段时通用心跳不能豁免过期执行",
        )
        assert_true(
            heartbeat_only_named["盘中止损巡检"].severity == "critical",
            "没有活跃阶段时通用心跳不能豁免过期止损",
        )

        long_cycle_state = dict(fresh_state)
        long_cycle_state.update({
            "last_scan_at": now_ts - 2000,
            "last_execute_at": now_ts - 2000,
            "last_stop_check_at": now_ts - 500,
            "updated_at": "2026-06-09T10:04:50",
            "active_stage": "scan",
            "stage_started_at": now_ts - 100,
            "stage_budget_seconds": 180,
        })
        _write_state(state_path, long_cycle_state)
        long_cycle_items = run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
            max_loop_lag_sec=600,
            max_scan_lag_sec=300,
            max_stop_lag_sec=120,
        )
        long_cycle_named = _by_name(long_cycle_items)
        assert_true(long_cycle_named["自动循环新鲜度"].ok, "长阶段期间循环状态保持新鲜")
        assert_true(long_cycle_named["盘中扫描"].ok, "预算内扫描阶段可临时豁免扫描新鲜度")
        assert_true(
            long_cycle_named["盘中模拟执行"].severity == "critical",
            "扫描阶段不能同时豁免模拟执行",
        )
        assert_true(
            long_cycle_named["盘中止损巡检"].severity == "critical",
            "扫描阶段不能同时豁免止损巡检",
        )

        execute_stage_state = dict(long_cycle_state)
        execute_stage_state.update({
            "active_stage": "execute_trades",
            "stage_started_at": now_ts - 100,
            "stage_budget_seconds": 180,
        })
        _write_state(state_path, execute_stage_state)
        execute_stage_named = _by_name(run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
            max_loop_lag_sec=600,
            max_scan_lag_sec=300,
            max_stop_lag_sec=120,
        ))
        assert_true(execute_stage_named["盘中模拟执行"].ok, "预算内执行阶段只豁免模拟执行")
        assert_true(execute_stage_named["盘中扫描"].severity == "critical", "执行阶段不能豁免扫描")
        assert_true(execute_stage_named["盘中止损巡检"].severity == "critical", "执行阶段不能豁免止损")

        stop_stage_state = dict(long_cycle_state)
        stop_stage_state.update({
            "active_stage": "stop_check",
            "stage_started_at": now_ts - 100,
            "stage_budget_seconds": 180,
        })
        _write_state(state_path, stop_stage_state)
        stop_stage_named = _by_name(run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
            max_loop_lag_sec=600,
            max_scan_lag_sec=300,
            max_stop_lag_sec=120,
        ))
        assert_true(stop_stage_named["盘中止损巡检"].ok, "预算内止损阶段只豁免止损巡检")
        assert_true(stop_stage_named["盘中扫描"].severity == "critical", "止损阶段不能豁免扫描")
        assert_true(stop_stage_named["盘中模拟执行"].severity == "critical", "止损阶段不能豁免执行")

        expired_stage_state = dict(long_cycle_state)
        expired_stage_state.update({
            "stage_started_at": now_ts - 181,
            "stage_budget_seconds": 180,
        })
        _write_state(state_path, expired_stage_state)
        expired_stage_items = run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
            max_loop_lag_sec=600,
            max_scan_lag_sec=300,
            max_stop_lag_sec=120,
        )
        expired_stage_scan = _by_name(expired_stage_items)["盘中扫描"]
        assert_true(
            expired_stage_scan.severity == "critical",
            "扫描阶段超过自身预算后立即恢复严格新鲜度检查",
        )
        assert_true("已超预算" in expired_stage_scan.detail, "超预算原因会在Watchdog中可见")

        expanded_budget_state = dict(long_cycle_state)
        expanded_budget_state.update({
            "stage_started_at": now_ts - AUTO_STAGE_BUDGET_SECONDS["scan"] - 1,
            "stage_budget_seconds": AUTO_STAGE_BUDGET_SECONDS["scan"] * 10,
        })
        _write_state(state_path, expanded_budget_state)
        expanded_budget_scan = _by_name(run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
            max_loop_lag_sec=600,
            max_scan_lag_sec=300,
            max_stop_lag_sec=120,
        ))["盘中扫描"]
        assert_true(
            expanded_budget_scan.severity == "critical",
            "状态文件不能自行扩大代码定义的阶段预算",
        )

        stale_state = dict(fresh_state)
        stale_state.update({
            "last_scan_at": now_ts - 2000,
            "last_execute_at": now_ts - 2000,
            "last_stop_check_at": now_ts - 500,
            "updated_at": "2026-06-09T09:00:00",
        })
        _write_state(state_path, stale_state)
        stale_items = run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
            max_loop_lag_sec=300,
            max_scan_lag_sec=300,
            max_stop_lag_sec=120,
        )
        stale_named = _by_name(stale_items)
        assert_true(stale_named["自动循环新鲜度"].severity == "critical", "自动循环停滞触发critical")
        assert_true(stale_named["盘中扫描"].severity == "critical", "盘中漏扫触发critical")
        assert_true(stale_named["盘中模拟执行"].severity == "critical", "盘中漏执行触发critical")
        assert_true(stale_named["盘中止损巡检"].severity == "critical", "盘中漏止损巡检触发critical")

        _write_lock(lock_path, {
            "pid": 12345,
            "token": "watchdog-test",
            "updated_at": "2026-06-09T09:00:00",
        }, mtime=time.time() - 3600)
        stale_lock_items = run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
            max_loop_lag_sec=300,
            max_scan_lag_sec=300,
            max_stop_lag_sec=120,
        )
        assert_true(_by_name(stale_lock_items)["自动盘锁"].severity == "critical", "自动盘锁过期触发critical")

        after_state = dict(fresh_state)
        after_state.update({
            "last_review_date": "2026-06-08",
            "updated_at": "2026-06-09T15:06:00",
        })
        _write_state(state_path, after_state)
        after_items = run_auto_watchdog(
            now=datetime(2026, 6, 9, 15, 10, 0),
            status_override="盘后",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
            max_loop_lag_sec=300,
        )
        after_named = _by_name(after_items)
        assert_true(after_named["盘后复盘进化"].severity == "critical", "盘后漏复盘触发critical")

        with Database(db_path=db_path) as db:
            db.insert_auto_event({
                "date": "2026-06-09",
                "status": "盘中",
                "actions": ["异常: 行情源失败"],
                "error": "行情源失败",
                "created_at": "2026-06-09T10:06:00",
            })
        error_items = run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
        )
        assert_true(_by_name(error_items)["今日异常事件"].severity == "critical", "auto_events异常触发critical")

        with Database(db_path=db_path) as db:
            db.insert_auto_event({
                "date": "2026-06-09",
                "status": "盘中",
                "actions": ["盘中扫描: 候选3只 决策2条", "模拟执行: 成交0笔 风控0项 错误0项"],
                "error": "",
                "created_at": "2026-06-09T10:07:00",
            })
        _write_state(state_path, fresh_state)
        recovered_items = run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
        )
        assert_true(
            _by_name(recovered_items)["今日异常事件"].severity == "warn",
            "历史异常被后续健康循环覆盖后降级为warn",
        )

        error_state = dict(fresh_state)
        error_state["last_error"] = "行情源失败"
        _write_state(state_path, error_state)
        unresolved_items = run_auto_watchdog(
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            lock_file=lock_path,
            db_path=db_path,
        )
        assert_true(
            _by_name(unresolved_items)["今日异常事件"].severity == "critical",
            "状态文件仍有last_error时保持critical",
        )

        print("自动盯盘Watchdog测试通过")

    finally:
        for path in temp_paths:
            for candidate in [path, f"{path}-wal", f"{path}-shm"]:
                if os.path.exists(candidate):
                    os.unlink(candidate)


if __name__ == "__main__":
    main()
