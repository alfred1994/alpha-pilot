#!/usr/bin/env python3
"""
自动盯盘运维状态汇总测试

使用临时SQLite、状态文件和控制文件，验证一页式状态汇总能够同时聚合：
健康检查、Watchdog、AI交易员报告和控制开关。
"""
import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from scheduler.control import resume_auto_trader
from scheduler.ops_status import format_ops_status, run_ops_status


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _write_state(path: str, state: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    temp_paths = []
    output_dir = tempfile.mkdtemp(prefix="quant_ops_status_")
    try:
        db_file = tempfile.NamedTemporaryFile(suffix="_quant.db", delete=False)
        db_path = db_file.name
        db_file.close()
        os.unlink(db_path)
        temp_paths.append(db_path)

        state_file = tempfile.NamedTemporaryFile(suffix="_auto_state.json", delete=False)
        state_path = state_file.name
        state_file.close()
        temp_paths.append(state_path)

        control_file = tempfile.NamedTemporaryFile(suffix="_auto_control.json", delete=False)
        control_path = control_file.name
        control_file.close()
        os.unlink(control_path)
        temp_paths.append(control_path)
        resume_auto_trader("运维状态测试", control_file=control_path)

        now = datetime(2026, 6, 9, 10, 5, 0)
        now_ts = now.timestamp()
        _write_state(state_path, {
            "date": "2026-06-09",
            "last_prefetch_date": "2026-06-09",
            "last_regime_date": "2026-06-09",
            "last_scan_at": now_ts - 60,
            "last_execute_at": now_ts - 50,
            "last_stop_check_at": now_ts - 20,
            "last_review_date": "",
            "loop_count": 3,
            "last_status": "盘中",
            "last_error": "",
            "updated_at": "2026-06-09T10:04:50",
        })

        with Database(db_path=db_path) as db:
            db.save_account_state({
                "initial_capital": 1000000,
                "cash": 990000,
                "total_assets": 1003000,
                "positions": {"600519": {"shares": 100}},
                "updated_at": "2026-06-09T10:04:00",
            })
            db.insert_auto_event({
                "date": "2026-06-09",
                "event_type": "auto_cycle",
                "status": "盘中",
                "actions": [
                    "止损巡检: 持仓1只 卖出0笔",
                    "盘中扫描: 候选2只 决策1条",
                    "模拟执行: 成交1笔 风控0项 错误0项",
                ],
                "created_at": "2026-06-09T10:04:00",
            })
            db.insert_trade({
                "code": "600519",
                "name": "贵州茅台",
                "action": "BUY",
                "price": 10,
                "shares": 100,
                "amount": 1000,
                "reason": "运维状态测试买入",
                "created_at": "2026-06-09T10:04:20",
            })
            db.insert_llm_decision({
                "code": "600519",
                "date": "2026-06-09",
                "action": "BUY",
                "reasoning": "运维状态测试决策",
                "confidence": 0.8,
                "created_at": "2026-06-09T10:04:10",
            })
            db.insert_lesson({
                "date": "2026-06-09",
                "category": "buy",
                "content": "运维状态测试教训。",
                "importance": 3,
                "created_at": "2026-06-09T15:10:00",
            })
            db.save_review_snapshot("2026-06-09", {"date": "2026-06-09"})

        result = run_ops_status(
            report_days=1,
            report_end_date="2026-06-09",
            db_path=db_path,
            output_dir=output_dir,
            state_file=state_path,
            control_file=control_path,
            now=now,
            status_override="盘中",
            trading_day_override=True,
            max_loop_lag_sec=180,
            max_scan_lag_sec=180,
            max_stop_lag_sec=180,
            save_report=True,
        )
        text = format_ops_status(result)

        assert_true(result["overall"]["level"] in ("ok", "warn"), "汇总状态没有critical")
        assert_true(not result["overall"]["paused"], "汇总包含运行中的控制状态")
        assert_true(result["report"]["metrics"]["auto_cycle_count"] == 1, "汇总包含自动循环统计")
        assert_true(result["report"]["metrics"]["closed_loop_days"] == 1, "汇总识别闭环日")
        assert_true(os.path.exists(result["report"]["paths"]["markdown"]), "汇总生成Markdown报告")
        assert_true("自动盯盘运维状态" in text, "格式化输出包含标题")
        assert_true("总体状态:" in text, "格式化输出包含总体状态")
        assert_true("状态归因:" in text, "格式化输出包含状态归因")
        assert_true("盘中观察:" in text, "格式化输出包含盘中观察")
        assert_true("下一轮参数:" in text, "格式化输出包含下一轮交易参数")
        assert_true("执行审计:" in text, "格式化输出包含执行审计")
        assert_true("今日闭环缺口:" in text, "格式化输出包含日内闭环缺口")
        assert_true("建议动作:" in text, "格式化输出包含建议动作")
        assert_true(result["next_actions"], "汇总生成下一步动作")

        _write_state(state_path, {
            "date": "2026-06-09",
            "last_prefetch_date": "2026-06-09",
            "last_regime_date": "2026-06-09",
            "last_scan_at": now_ts - 3600,
            "last_execute_at": now_ts - 3600,
            "last_stop_check_at": now_ts - 3600,
            "last_review_date": "",
            "loop_count": 4,
            "last_status": "盘中",
            "last_error": "",
            "updated_at": "2026-06-09T09:00:00",
        })
        stale = run_ops_status(
            report_days=1,
            report_end_date="2026-06-09",
            db_path=db_path,
            output_dir=output_dir,
            state_file=state_path,
            control_file=control_path,
            now=now,
            status_override="盘中",
            trading_day_override=True,
            max_loop_lag_sec=180,
            max_scan_lag_sec=180,
            max_stop_lag_sec=180,
            save_report=False,
        )
        stale_text = format_ops_status(stale)
        assert_true(stale["overall"]["level"] == "critical", "停滞状态汇总为critical")
        assert_true(any("restart_auto.ps1" in a.get("command", "") for a in stale["next_actions"]), "停滞状态建议重启自动盘长驻任务")
        assert_true(any("--doctor --force-scan" in a.get("command", "") for a in stale["next_actions"]), "停滞状态建议Doctor自愈")
        assert_true("重启自动盘长驻任务" in stale_text, "停滞状态报告显示长驻重启动作")
        assert_true("触发Doctor自愈" in stale_text, "停滞状态报告显示自愈动作")

        print("自动盯盘运维状态汇总测试通过")

    finally:
        for path in temp_paths:
            for candidate in [path, f"{path}-wal", f"{path}-shm"]:
                if os.path.exists(candidate):
                    os.unlink(candidate)
        if os.path.isdir(output_dir):
            for name in os.listdir(output_dir):
                os.unlink(os.path.join(output_dir, name))
            os.rmdir(output_dir)


if __name__ == "__main__":
    main()
