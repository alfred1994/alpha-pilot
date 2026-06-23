#!/usr/bin/env python3
"""
自动盯盘Doctor测试

构造盘中停滞状态，验证Doctor会触发一轮自动循环自愈，
复查后清除critical，并记录auto_doctor事件。
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.database import Database
from scheduler.control import get_auto_control_state, pause_auto_trader
from scheduler.doctor import run_auto_doctor
from scheduler.pipeline import PipelineResult, StepResult


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  OK {message}")


def _write_state(path: str, state: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    temp_paths = []
    try:
        state_file = tempfile.NamedTemporaryFile(suffix="_auto_state.json", delete=False)
        state_path = state_file.name
        state_file.close()
        temp_paths.append(state_path)

        db_file = tempfile.NamedTemporaryFile(suffix="_quant.db", delete=False)
        db_path = db_file.name
        db_file.close()
        os.unlink(db_path)
        temp_paths.append(db_path)

        control_file = tempfile.NamedTemporaryFile(suffix="_auto_control.json", delete=False)
        control_path = control_file.name
        control_file.close()
        os.unlink(control_path)
        temp_paths.append(control_path)

        now = datetime.now().replace(microsecond=0)
        today = now.strftime("%Y-%m-%d")
        stale_ts = now.timestamp() - 3600
        _write_state(state_path, {
            "date": today,
            "last_prefetch_date": today,
            "last_regime_date": today,
            "last_scan_at": stale_ts,
            "last_execute_at": stale_ts,
            "last_stop_check_at": stale_ts,
            "last_review_date": "",
            "loop_count": 7,
            "last_status": "盘中",
            "last_error": "",
            "updated_at": (now.replace(hour=max(0, now.hour - 1))).isoformat(),
        })

        calls = []

        def fake_check_stops_once():
            calls.append("check_stops_once")
            return {"checked": 1, "sold": 0, "trades": []}

        def fake_run_scan():
            calls.append("run_scan")
            return PipelineResult(
                date=today,
                market_status="盘中",
                steps=[StepResult(name="doctor_scan", success=True)],
                candidates=[{"code": "600519"}],
                decisions=[SimpleNamespace(code="600519", action="BUY")],
            )

        def fake_execute_trades():
            calls.append("execute_trades")
            return PipelineResult(
                date=today,
                market_status="盘中",
                steps=[StepResult(name="doctor_execute", success=True)],
                executed_orders=[{"code": "600519", "action": "BUY"}],
            )

        result = run_auto_doctor(
            recover=True,
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            db_path=db_path,
            control_file=control_path,
            services={
                "check_stops_once": fake_check_stops_once,
                "run_scan": fake_run_scan,
                "execute_trades": fake_execute_trades,
            },
            repair_closure=False,
            max_loop_lag_sec=120,
            max_scan_lag_sec=120,
            max_stop_lag_sec=120,
        )

        assert_true(result["before_critical"], "Doctor识别修复前critical")
        assert_true(result["after_critical"] == [], "Doctor自愈后critical清零")
        assert_true(result["recovered"], "Doctor标记已恢复")
        assert_true(calls == ["check_stops_once", "run_scan", "execute_trades"], "Doctor触发一轮盘中自愈循环")

        with Database(db_path=db_path) as db:
            doctor_events = db.get_auto_events(event_type="auto_doctor", limit=10)
            cycle_events = db.get_auto_events(event_type="auto_cycle", limit=10)
        assert_true(len(doctor_events) == 1, "Doctor事件写入SQLite")
        assert_true(len(cycle_events) == 1, "自愈自动循环事件写入SQLite")
        assert_true(doctor_events[0]["details"]["after_critical"] == [], "Doctor事件记录修复后critical")

        # 构造自愈失败：扫描服务抛错，Doctor应自动暂停盘中交易动作。
        _write_state(state_path, {
            "date": today,
            "last_prefetch_date": today,
            "last_regime_date": today,
            "last_scan_at": stale_ts,
            "last_execute_at": stale_ts,
            "last_stop_check_at": stale_ts,
            "last_review_date": "",
            "loop_count": 8,
            "last_status": "盘中",
            "last_error": "",
            "updated_at": (now.replace(hour=max(0, now.hour - 1))).isoformat(),
        })

        def broken_run_scan():
            raise RuntimeError("演练扫描失败")

        failed = run_auto_doctor(
            recover=True,
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=state_path,
            db_path=db_path,
            control_file=control_path,
            services={
                "check_stops_once": fake_check_stops_once,
                "run_scan": broken_run_scan,
                "execute_trades": fake_execute_trades,
            },
            repair_closure=False,
            max_loop_lag_sec=120,
            max_scan_lag_sec=120,
            max_stop_lag_sec=120,
        )
        control_state = get_auto_control_state(control_path)
        assert_true(failed["after_critical"], "自愈失败后仍有critical")
        assert_true(control_state["paused"], "自愈失败后自动暂停盘中交易动作")
        assert_true("doctor unresolved critical" in control_state["reason"], "自动暂停原因记录critical")

        # Watchdog正常时，Doctor仍会做闭环自愈检查，并把结果写入审计事件。
        clean_state_file = tempfile.NamedTemporaryFile(suffix="_auto_state_clean.json", delete=False)
        clean_state_path = clean_state_file.name
        clean_state_file.close()
        temp_paths.append(clean_state_path)
        clean_db_file = tempfile.NamedTemporaryFile(suffix="_quant_clean.db", delete=False)
        clean_db_path = clean_db_file.name
        clean_db_file.close()
        os.unlink(clean_db_path)
        temp_paths.append(clean_db_path)
        clean_control_file = tempfile.NamedTemporaryFile(suffix="_auto_control_clean.json", delete=False)
        clean_control_path = clean_control_file.name
        clean_control_file.close()
        os.unlink(clean_control_path)
        temp_paths.append(clean_control_path)
        doctor_paused_control_file = tempfile.NamedTemporaryFile(suffix="_auto_control_doctor_paused.json", delete=False)
        doctor_paused_control_path = doctor_paused_control_file.name
        doctor_paused_control_file.close()
        os.unlink(doctor_paused_control_path)
        temp_paths.append(doctor_paused_control_path)
        manual_paused_control_file = tempfile.NamedTemporaryFile(suffix="_auto_control_manual_paused.json", delete=False)
        manual_paused_control_path = manual_paused_control_file.name
        manual_paused_control_file.close()
        os.unlink(manual_paused_control_path)
        temp_paths.append(manual_paused_control_path)

        _write_state(clean_state_path, {
            "date": today,
            "last_prefetch_date": today,
            "last_regime_date": today,
            "last_scan_at": now.timestamp(),
            "last_execute_at": now.timestamp(),
            "last_stop_check_at": now.timestamp(),
            "last_review_date": "",
            "loop_count": 10,
            "last_status": "盘中",
            "last_error": "",
            "updated_at": now.isoformat(),
        })
        with Database(db_path=clean_db_path) as db:
            db.insert_auto_event({
                "date": today,
                "event_type": "auto_cycle",
                "status": "盘中",
                "actions": ["止损巡检: 持仓1只 卖出0笔", "盘中扫描: 候选1只 决策1条", "模拟执行: 成交1笔 风控0项 错误0项"],
                "created_at": now.isoformat(),
            })

        pause_auto_trader(
            reason="doctor unresolved critical: 自动盘锁",
            control_file=doctor_paused_control_path,
            updated_by="auto_doctor",
        )
        doctor_resume_result = run_auto_doctor(
            recover=True,
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=clean_state_path,
            db_path=clean_db_path,
            control_file=doctor_paused_control_path,
            repair_closure=False,
            max_loop_lag_sec=120,
            max_scan_lag_sec=120,
            max_stop_lag_sec=120,
        )
        doctor_resume_state = get_auto_control_state(doctor_paused_control_path)
        assert_true(not doctor_resume_state["paused"], "Watchdog正常后恢复Doctor遗留暂停")
        assert_true(doctor_resume_result["pause_state"]["paused"] is False, "Doctor恢复状态写入结果")
        doctor_control_item = [
            item for item in doctor_resume_result["after_items"]
            if item.name == "自动交易控制"
        ][0]
        assert_true("运行" in doctor_control_item.detail, "Doctor恢复后Watchdog报告显示运行")

        pause_auto_trader(
            reason="manual safety pause",
            control_file=manual_paused_control_path,
            updated_by="manual",
        )
        manual_pause_result = run_auto_doctor(
            recover=True,
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=clean_state_path,
            db_path=clean_db_path,
            control_file=manual_paused_control_path,
            repair_closure=False,
            max_loop_lag_sec=120,
            max_scan_lag_sec=120,
            max_stop_lag_sec=120,
        )
        manual_pause_state = get_auto_control_state(manual_paused_control_path)
        assert_true(manual_pause_state["paused"], "Doctor不会恢复人工暂停")
        assert_true(manual_pause_result["pause_state"] is None, "人工暂停不会被写成Doctor恢复")

        closure_calls = []

        def fake_closure_repair(**kwargs):
            closure_calls.append(kwargs)
            return {
                "gap": {"name": "盘中扫描"},
                "before": {"overall": {"level": "warn", "critical": [], "warn": ["盘中扫描"]}},
                "after": {"overall": {"level": "ok", "critical": [], "warn": []}},
            }

        closure_result = run_auto_doctor(
            recover=True,
            now=now,
            status_override="盘中",
            trading_day_override=True,
            state_file=clean_state_path,
            db_path=clean_db_path,
            control_file=clean_control_path,
            repair_closure=True,
            closure_repair_func=fake_closure_repair,
            max_loop_lag_sec=120,
            max_scan_lag_sec=120,
            max_stop_lag_sec=120,
        )
        assert_true(closure_calls, "Doctor在Watchdog正常后执行闭环自愈检查")
        assert_true(closure_result["closure_repair"]["after"]["overall"]["level"] == "ok", "Doctor返回闭环自愈结果")
        with Database(db_path=clean_db_path) as db:
            latest_doctor = db.get_auto_events(event_type="auto_doctor", limit=1)[0]
        assert_true(latest_doctor["details"]["closure_gap"] == "盘中扫描", "Doctor事件记录闭环缺口")
        assert_true(latest_doctor["details"]["closure_after_level"] == "ok", "Doctor事件记录闭环自愈后等级")

        print("自动盯盘Doctor测试通过")

    finally:
        for path in temp_paths:
            for candidate in [path, f"{path}-wal", f"{path}-shm"]:
                if os.path.exists(candidate):
                    os.unlink(candidate)


if __name__ == "__main__":
    main()
