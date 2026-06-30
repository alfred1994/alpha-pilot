#!/usr/bin/env python3
"""
模拟盘AI交易员就绪检查测试

用注入的健康、无人值守、正式报告和演练报告数据验证：
  - 演练闭环可作为启动门禁证据
  - 无人值守未完全安装时是warn而非critical
  - 格式化报告给出可手动启动但仍需处理warn的结论
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from scheduler.health import HealthItem
from scheduler.readiness import _load_rehearsal_report, format_paper_readiness, run_paper_readiness
from scheduler.windows_tasks import UnattendedItem


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _report(days, cycles, closed_loop, trades, lessons, adaptive=True, errors=0):
    return {
        "metrics": {
            "days": days,
            "auto_cycle_count": cycles,
            "closed_loop_days": closed_loop,
            "trade_count": trades,
            "lesson_count": lessons,
            "error_events": errors,
        },
        "adaptive": {"available": adaptive},
    }


def main():
    health = [
        HealthItem("依赖:pandas", True, "已安装", True),
        HealthItem("SQLite数据库", True, "可用", True),
        HealthItem("交易通道", True, "mode=paper", True),
    ]
    unattended = [
        UnattendedItem("脚本:run_auto", True, "ok", "run_auto.ps1"),
        UnattendedItem("计划任务:Auto", False, "warn", "未安装: QuantPilot-Auto"),
        UnattendedItem("日志:Auto", False, "warn", "暂无日志"),
    ]
    formal_report = _report(days=5, cycles=3, closed_loop=0, trades=0, lessons=0, adaptive=False)
    rehearsal_report = _report(days=3, cycles=9, closed_loop=3, trades=6, lessons=3, adaptive=True)

    result = run_paper_readiness(
        health_items=health,
        unattended_items=unattended,
        formal_report=formal_report,
        rehearsal_report=rehearsal_report,
        rehearsal_days=3,
    )
    text = format_paper_readiness(result)

    assert_true(result["overall"]["ready"], "无critical时允许手动启动观察")
    assert_true(not result["overall"]["fully_unattended_ready"], "存在warn时不算完全无人值守就绪")
    assert_true("无人值守配置" in result["overall"]["warn"], "无人值守warn进入汇总")
    assert_true(any(i.name == "连续演练闭环" and i.ok for i in result["items"]), "演练闭环检查通过")
    assert_true("可手动启动模拟盘观察" in text, "报告给出手动启动结论")

    broken = run_paper_readiness(
        health_items=health,
        unattended_items=[],
        formal_report=formal_report,
        rehearsal_report=_report(days=3, cycles=3, closed_loop=0, trades=0, lessons=0, adaptive=False),
        rehearsal_days=3,
    )
    assert_true(not broken["overall"]["ready"], "演练闭环不足会阻止启动")
    assert_true("连续演练闭环" in broken["overall"]["critical"], "演练失败进入critical")

    db_file = tempfile.NamedTemporaryFile(suffix="_rehearsal.db", delete=False)
    db_path = db_file.name
    db_file.close()
    os.unlink(db_path)
    try:
        with Database(db_path=db_path) as db:
            for date in ["2026-06-09", "2026-06-10", "2026-06-11"]:
                db.insert_auto_event({
                    "date": date,
                    "status": "盘中",
                    "actions": ["盘中扫描: 候选1只 决策1条", "模拟执行: 成交1笔 风控0项 错误0项"],
                    "created_at": f"{date}T10:00:00",
                })
                db.insert_trade({
                    "code": "600519",
                    "name": "贵州茅台",
                    "action": "BUY",
                    "price": 10,
                    "shares": 100,
                    "amount": 1000,
                    "created_at": f"{date}T10:01:00",
                })
                db.insert_llm_decision({
                    "code": "600519",
                    "date": date,
                    "action": "BUY",
                    "outcome": "win",
                    "outcome_pct": 1.0,
                    "created_at": f"{date}T10:00:30",
                })
                db.insert_lesson({
                    "date": date,
                    "category": "buy",
                    "content": "就绪测试教训",
                    "importance": 3,
                    "created_at": f"{date}T15:10:00",
                })
                db.save_review_snapshot(date, {"date": date})
        loaded = _load_rehearsal_report(days=3, db_path=db_path)
        assert_true(loaded["end_date"] == "2026-06-11", "演练报告默认使用库内最新日期")
    finally:
        for candidate in [db_path, f"{db_path}-wal", f"{db_path}-shm"]:
            if os.path.exists(candidate):
                os.unlink(candidate)

    print("模拟盘AI交易员就绪检查测试通过")


if __name__ == "__main__":
    main()
