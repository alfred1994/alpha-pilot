#!/usr/bin/env python3
"""
正式模拟盘闭环缺口自愈测试

验证闭环修复器只在合适窗口触发安全动作：
  - 盘中缺扫描：调用paper-observe强制扫描
  - 盘后缺复盘：调用run_review
  - 非盘中缺扫描：等待，不乱触发
"""
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scheduler.closure_repair import format_closure_repair, run_closure_repair


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _closure(status, gap_name, level="critical"):
    return {
        "date": "2026-06-09",
        "market_status": status,
        "items": [{
            "name": gap_name,
            "ok": False,
            "severity": level,
            "detail": f"缺口 {gap_name}",
            "suggestion": "测试建议",
        }],
        "overall": {
            "level": level,
            "critical": [gap_name] if level == "critical" else [],
            "warn": [gap_name] if level == "warn" else [],
            "closed_loop": False,
        },
    }


def _temp_db():
    db_file = tempfile.NamedTemporaryFile(suffix="_closure_repair.db", delete=False)
    db_path = db_file.name
    db_file.close()
    os.unlink(db_path)
    return db_path


def _cleanup_db(path: str):
    for candidate in [path, f"{path}-wal", f"{path}-shm"]:
        if os.path.exists(candidate):
            os.unlink(candidate)


def main():
    db_path = _temp_db()
    try:
        calls = []
        before_scan = _closure("盘中", "盘中扫描")
        after_scan = _closure("盘中", "盘后复盘", "warn")

        def scan_closure_func(**kwargs):
            calls.append(("closure", kwargs.get("date")))
            closure_calls = [c for c in calls if c[0] == "closure"]
            return before_scan if len(closure_calls) == 1 else after_scan

        def fake_observe(**kwargs):
            calls.append(("observe", kwargs))
            return {"status": "ok"}

        repaired = run_closure_repair(
            date="2026-06-09",
            now=datetime(2026, 6, 9, 10, 0, 0),
            db_path=db_path,
            closure_func=scan_closure_func,
            observe_func=fake_observe,
        )
        text = format_closure_repair(repaired)
        observe_calls = [c for c in calls if c[0] == "observe"]
        assert_true(observe_calls, "盘中缺扫描触发观察")
        assert_true(observe_calls[0][1]["force_scan"], "观察使用强制扫描")
        assert_true(repaired["gap"]["name"] == "盘中扫描", "记录被修复缺口")
        assert_true("正式模拟盘闭环自愈" in text, "格式化输出包含标题")

        calls = []
        before_review = _closure("盘后", "盘后复盘")
        after_review = {
            "date": "2026-06-09",
            "market_status": "盘后",
            "items": [],
            "overall": {"level": "ok", "critical": [], "warn": [], "closed_loop": True},
        }

        def review_closure_func(**kwargs):
            calls.append(("closure", kwargs.get("date")))
            closure_calls = [c for c in calls if c[0] == "closure"]
            return before_review if len(closure_calls) == 1 else after_review

        def fake_review():
            calls.append(("review", {}))
            return {"status": "ok"}

        review_repaired = run_closure_repair(
            date="2026-06-09",
            now=datetime(2026, 6, 9, 15, 10, 0),
            db_path=db_path,
            closure_func=review_closure_func,
            review_func=fake_review,
        )
        assert_true(any(c[0] == "review" for c in calls), "盘后缺复盘触发复盘")
        assert_true(review_repaired["after"]["overall"]["level"] == "ok", "复盘后闭环等级改善")

        calls = []
        wait_closure = _closure("盘后", "盘中扫描", "warn")

        def wait_closure_func(**kwargs):
            calls.append(("closure", kwargs.get("date")))
            return wait_closure

        wait_result = run_closure_repair(
            date="2026-06-09",
            now=datetime(2026, 6, 9, 15, 30, 0),
            db_path=db_path,
            closure_func=wait_closure_func,
            observe_func=fake_observe,
        )
        assert_true(not any(c[0] == "observe" for c in calls), "非盘中缺扫描不触发观察")
        assert_true(any("等待" in action for action in wait_result["actions"]), "非窗口动作记录等待")

        print("正式模拟盘闭环缺口自愈测试通过")
    finally:
        _cleanup_db(db_path)


if __name__ == "__main__":
    main()
