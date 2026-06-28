#!/usr/bin/env python3
"""
自动盯盘连续演练测试

验证演练器能在隔离数据库中跑出多日自动盯盘闭环：
盘前 → 盘中扫描/模拟成交 → 盘后复盘 → 教训沉淀 → 自适应纠偏。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.database import Database
from scheduler.rehearsal import run_auto_rehearsal


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _remove_tree(path):
    if not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.unlink(os.path.join(root, name))
        for name in dirs:
            os.rmdir(os.path.join(root, name))
    os.rmdir(path)


def main():
    temp_dir = tempfile.mkdtemp(prefix="quant_rehearsal_")
    db_path = os.path.join(temp_dir, "rehearsal.db")
    output_dir = os.path.join(temp_dir, "reports")

    try:
        result = run_auto_rehearsal(
            days=3,
            start_date="2026-06-01",
            db_path=db_path,
            output_dir=output_dir,
            save_report=True,
        )
        report = result["report"]
        metrics = report["metrics"]

        assert_true(result["status"] == "ok", "连续演练执行成功")
        assert_true(len(result["cycles"]) == 9, "3天演练产生9轮自动循环")
        assert_true(metrics["auto_cycle_count"] == 9, "报告统计9轮自动循环")
        assert_true(metrics["active_days"] == 3, "报告覆盖3个活跃日")
        assert_true(metrics["closed_loop_days"] == 3, "3天都形成完整闭环")
        assert_true(metrics["scan_count"] == 3, "每天都有盘中扫描")
        assert_true(metrics["execute_count"] == 3, "每天都有模拟执行")
        assert_true(metrics["trade_count"] == 6, "每天产生买入和卖出演练成交")
        assert_true(metrics["llm_decision_count"] == 3, "每天保存LLM决策")
        assert_true(metrics["lesson_count"] == 3, "每天沉淀复盘教训")
        assert_true(report["adaptive"]["available"], "演练后写入自适应状态")
        assert_true(report["adaptive"]["current_top_k_delta"] == -1, "演练触发TopK收紧")
        assert_true(report["adaptive"]["current_position_scale"] == 0.8, "演练触发仓位缩放")
        assert_true(os.path.exists(report["paths"]["markdown"]), "演练Markdown报告已保存")

        with Database(db_path=db_path) as db:
            events = db.get_auto_events(limit=20)
            auto_cycle_events = [
                event for event in events
                if event.get("event_type", "auto_cycle") == "auto_cycle"
            ]
            adaptive = db.get_adaptive_state()
        assert_true(len(auto_cycle_events) == 9, "隔离数据库写入9条自动盯盘主循环事件")
        assert_true(adaptive["current_buy_threshold"] == 62, "隔离数据库保存自适应买入阈值")

        second = run_auto_rehearsal(
            days=2,
            start_date="2026-06-10",
            db_path=db_path,
            output_dir=output_dir,
            save_report=True,
        )
        second_metrics = second["report"]["metrics"]
        assert_true(second_metrics["auto_cycle_count"] == 6, "重复演练会重置隔离数据库")
        assert_true(second_metrics["closed_loop_days"] == 2, "重复演练报告只统计本次日期")

        print("自动盯盘连续演练测试通过")

    finally:
        _remove_tree(temp_dir)


if __name__ == "__main__":
    main()
