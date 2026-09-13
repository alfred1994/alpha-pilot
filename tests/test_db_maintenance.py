#!/usr/bin/env python3
"""数据库维护任务回归测试。"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.db_maintenance import run_db_maintenance


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
        if os.path.exists(path):
            os.unlink(path)


def _make_db(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""CREATE TABLE auto_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, event_type TEXT,
        status TEXT, actions TEXT, details TEXT, error TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE trade_plan_executions (
        plan_id TEXT PRIMARY KEY, plan_date TEXT, payload_hash TEXT,
        status TEXT, claimed_at TEXT, completed_at TEXT, error TEXT)""")
    c.execute("INSERT INTO auto_events (created_at, event_type) VALUES ('2026-01-01T10:00:00', 'old')")
    c.execute("INSERT INTO auto_events (created_at, event_type) VALUES (?, 'fresh')",
              (__import__('datetime').datetime.now().isoformat(),))
    c.execute("INSERT INTO trade_plan_executions (plan_id, plan_date, status) VALUES ('p1', '2026-01-01', 'completed')")
    conn.commit()
    conn.close()


def test_maintenance_backup_and_prune():
    print("测试1: 备份+完整性+运维日志清理")
    db_path = temp_path("_maint.db")
    backup_dir = tempfile.mkdtemp(prefix="_bak_")
    try:
        _make_db(db_path)
        result = run_db_maintenance(
            db_path=db_path, backup=True, prune=True, vacuum=False,
            backup_dir=backup_dir,
        )
        assert_true(result["status"] == "ok", "维护状态ok")
        assert_true(result["integrity"]["ok"], "完整性检查通过")
        assert_true("backup" in result, "执行了备份")
        assert_true(result["pruned"]["auto_events"] == 1, "过期auto_events清理1条")
        assert_true(result["pruned"]["trade_plan_executions"] == 1, "过期计划执行清理1条")

        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM auto_events")
        assert_true(c.fetchone()[0] == 1, "新鲜记录保留")
        conn.close()

        backups = [f for f in os.listdir(backup_dir) if f.startswith("quant_")]
        assert_true(len(backups) == 1, f"备份文件存在({backups})")
    finally:
        cleanup(db_path)


def test_corrupt_db_refuses_prune():
    print("测试2: 损坏库拒绝清理")
    db_path = temp_path("_corrupt.db")
    backup_dir = tempfile.mkdtemp(prefix="_bak2_")
    try:
        with open(db_path, "wb") as f:
            f.write(b"this is not a sqlite database" * 100)
        result = run_db_maintenance(
            db_path=db_path, backup=False, prune=True, backup_dir=backup_dir,
        )
        assert_true(result["status"] == "corrupt", "损坏库标记corrupt")
        assert_true("pruned" not in result, "损坏库不执行清理")
    finally:
        cleanup(db_path)


def main():
    test_maintenance_backup_and_prune()
    test_corrupt_db_refuses_prune()
    print("\n数据库维护测试全部通过")


if __name__ == "__main__":
    main()
