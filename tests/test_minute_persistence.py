#!/usr/bin/env python3
"""盘中1分钟K线落盘与保留策略回归测试。"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from scheduler.intraday_watch import (
    MINUTE_BARS_RETENTION_DAYS,
    _minute_prune_state,
    _persist_minute_bars,
    _prune_minute_bars_if_needed,
)


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def temp_path(suffix):
    item = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    path = item.name
    item.close()
    os.unlink(path)
    return path


def cleanup(*paths):
    for path in paths:
        for candidate in (path, f"{path}-wal", f"{path}-shm"):
            if os.path.exists(candidate):
                os.unlink(candidate)


def _minute_frame(bars=5, code="600519", start=None):
    start = start or (datetime.now() - timedelta(minutes=bars))
    rows = []
    for i in range(bars):
        ts = start + timedelta(minutes=i)
        rows.append({
            "code": code, "datetime": ts.strftime("%Y-%m-%d %H:%M"),
            "open": 10.0 + i * 0.01, "close": 10.05 + i * 0.01,
            "high": 10.1 + i * 0.01, "low": 9.95 + i * 0.01,
            "volume_hand": 100 + i, "amount_yuan": 100000.0 + i,
            "data_valid": True,
        })
    return pd.DataFrame(rows)


def test_persist_and_idempotent():
    print("测试1: 分钟线落盘且重复写入幂等")
    db_path = temp_path("_minute.db")
    try:
        frame = _minute_frame()
        written = _persist_minute_bars("600519", frame, db_path=db_path)
        assert_true(written == 5, f"写入{written}条")

        with Database(db_path=db_path) as db:
            rows = db.get_k_minute("600519", "2000-01-01", "2100-01-01", period="1m")
            assert_true(len(rows) == 5, f"库中{len(rows)}条")

        # 重复写入同一批（TTL缓存过期后重新拉到重叠数据）
        written_again = _persist_minute_bars("600519", frame, db_path=db_path)
        assert_true(written_again == 5, "重复写入返回条数不变")
        with Database(db_path=db_path) as db:
            rows = db.get_k_minute("600519", "2000-01-01", "2100-01-01", period="1m")
            assert_true(len(rows) == 5, f"幂等后仍{len(rows)}条")

        # 无效数据帧不写入
        assert_true(_persist_minute_bars("600519", None, db_path=db_path) == 0, "空帧返回0")
        assert_true(_persist_minute_bars("600519", pd.DataFrame(), db_path=db_path) == 0, "空DataFrame返回0")
    finally:
        cleanup(db_path)


def test_retention_prune():
    print("测试2: 保留期清理")
    db_path = temp_path("_prune.db")
    try:
        old_ts = datetime.now() - timedelta(days=MINUTE_BARS_RETENTION_DAYS + 5)
        fresh_ts = datetime.now() - timedelta(minutes=5)
        old_frame = _minute_frame(bars=2, start=old_ts)
        fresh_frame = _minute_frame(bars=2, start=fresh_ts)
        _persist_minute_bars("600519", old_frame, db_path=db_path)
        _persist_minute_bars("600519", fresh_frame, db_path=db_path)

        # 重置"每天一次"状态，强制执行清理
        _minute_prune_state["last_date"] = None
        _prune_minute_bars_if_needed(db_path=db_path)

        with Database(db_path=db_path) as db:
            rows = db.get_k_minute("600519", "2000-01-01", "2100-01-01", period="1m")
            assert_true(len(rows) == 2, f"过期数据被清理，仅剩{len(rows)}条新鲜数据")
    finally:
        cleanup(db_path)
        _minute_prune_state["last_date"] = None


def test_invalid_bars_skipped():
    print("测试3: 无效行情bar不落盘")
    db_path = temp_path("_invalid.db")
    try:
        frame = _minute_frame(bars=3)
        frame.loc[0, "data_valid"] = False
        written = _persist_minute_bars("600519", frame, db_path=db_path)
        assert_true(written == 2, f"无效bar被跳过，写入{written}条")
    finally:
        cleanup(db_path)


def main():
    test_persist_and_idempotent()
    test_retention_prune()
    test_invalid_bars_skipped()
    print("\n分钟线落盘测试全部通过")


if __name__ == "__main__":
    main()
