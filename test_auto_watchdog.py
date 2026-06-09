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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.database import Database
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

        print("自动盯盘Watchdog测试通过")

    finally:
        for path in temp_paths:
            for candidate in [path, f"{path}-wal", f"{path}-shm"]:
                if os.path.exists(candidate):
                    os.unlink(candidate)


if __name__ == "__main__":
    main()
