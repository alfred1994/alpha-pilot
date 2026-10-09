"""长扫描、真实止损失败及交易互斥的回归演练，不调用外部行情或LLM。"""
import json
import os
import sys
import tempfile
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler.auto_trader import AUTO_TRADE_LOCK, AutoTraderState, run_auto_cycle
from scheduler.watchdog import run_auto_watchdog


def test_stops_continue_during_blocked_scan():
    with tempfile.TemporaryDirectory() as directory:
        state_path = os.path.join(directory, "state.json")
        control_path = os.path.join(directory, "control.json")
        with open(control_path, "w") as file:
            json.dump({"paused": False}, file)
        scanned = threading.Event()
        checked = threading.Event()
        calls = []
        now = datetime(2026, 10, 9, 10)
        now_ts = now.timestamp()

        def stops():
            calls.append("stop")
            if scanned.is_set():
                # 真正阻塞的扫描期间看到的持久状态必须仍是scan。
                with open(state_path, encoding="utf-8") as file:
                    snapshot = json.load(file)
                assert snapshot["active_stage"] == "scan"
                assert snapshot["stage_started_at"] == now_ts
                checked.set()
            return {"checked": 1, "sold": 0}

        def scan():
            scanned.set()
            assert checked.wait(2), "扫描阻塞期间止损仍应运行"
            return SimpleNamespace(candidates=[], decisions=[])

        def execute():
            calls.append("execute")
            assert checked.is_set()
            return SimpleNamespace(executed_orders=[], risk_triggered=[], errors=[])

        with patch("scheduler.auto_trader.AUTO_STOP_INTERVAL", 0.02), \
                patch("scheduler.auto_trader._record_auto_event") as events:
            result = run_auto_cycle(
                state=AutoTraderState(date="2026-10-09", last_watch_at=now_ts),
                status_override="盘中", trading_day_override=True, today_override="2026-10-09",
                now_override=now, now_ts_override=now_ts,
                services={"check_stops_once": stops, "run_scan": scan, "execute_trades": execute},
                persist_state=True, record_event=True, state_file=state_path,
                control_file=control_path, notify=False,
            )
        assert calls == ["stop", "stop", "execute"]
        assert not result["state"]["last_error"]
        assert result["state"]["active_stage"] == ""
        assert any(call.args[1].get("details", {}).get("during_stage") == "scan"
                   for call in events.call_args_list)


def test_failed_stop_during_rescue_prevents_execution():
    scanning = threading.Event()
    failed = threading.Event()
    exited = threading.Event()
    calls = []

    def stops():
        if scanning.is_set():
            failed.set()
            return {"error": "报价过期", "checked": 1, "sold": 0}
        return {"checked": 1, "sold": 0}

    def scan(**kwargs):
        scanning.set()
        assert failed.wait(2)
        exited.set()
        return SimpleNamespace(trade_plan={"orders": [{"code": "600519"}]})

    watch = {"actions": [], "details": {"top_watch": []}, "watchlist": {"items": {}},
             "missed_opportunity": False, "rescue_requested": True, "eligible_codes": ["600519"]}
    with patch("scheduler.auto_trader.AUTO_STOP_INTERVAL", 0.02), \
            patch("scheduler.control.get_auto_control_state", return_value={"paused": False}), \
            patch("scheduler.pipeline.run_scan", side_effect=scan), \
            patch("scheduler.intraday_watch.filter_trade_plan_for_rescue", side_effect=lambda plan, codes: plan), \
            patch("scheduler.pipeline.execute_trade_plan", side_effect=lambda *a, **k: calls.append("execute")):
        result = run_auto_cycle(
            state=AutoTraderState(date="2026-10-09"),
            status_override="盘中", trading_day_override=True, today_override="2026-10-09",
            now_override=datetime(2026, 10, 9, 10), now_ts_override=2000,
            services={"check_stops_once": stops, "run_watch_cycle": lambda **kwargs: watch},
            persist_state=False, record_event=False, notify=False,
        )
    assert exited.is_set(), "失败后必须先等待扫描线程结束，不能遗留交易线程"
    assert calls == [], "止损失败后不得执行救援计划"
    assert "报价过期" in result["state"]["last_error"]
    assert result["state"]["active_stage"] == ""


def test_rescue_execution_serialized_with_stop_check():
    import scheduler.auto_trader as trader
    invoked = threading.Event()
    finished = threading.Event()

    def execute(*args, **kwargs):
        invoked.set()
        return SimpleNamespace()

    def rescue():
        trader._run_rescue_scan_default({"eligible_codes": [], "details": {}},
                                       on_execute=lambda: None)
        finished.set()

    with patch("scheduler.pipeline.run_scan", return_value=SimpleNamespace(trade_plan={"orders": [1]})), \
            patch("scheduler.intraday_watch.filter_trade_plan_for_rescue", side_effect=lambda p, c: p), \
            patch("scheduler.pipeline.execute_trade_plan", side_effect=execute):
        with AUTO_TRADE_LOCK:
            worker = threading.Thread(target=rescue)
            worker.start()
            assert not invoked.wait(0.05), "止损持有交易锁时不得并发成交"
        worker.join(2)
        assert finished.is_set() and invoked.is_set()


def test_lunch_does_not_create_false_execute_timeout():
    with tempfile.TemporaryDirectory() as directory:
        state_path = os.path.join(directory, "state.json")
        state = {"date": "2026-10-09", "updated_at": "2026-10-09T13:02:00",
                 "last_scan_at": datetime(2026, 10, 9, 11, 2).timestamp(),
                 "last_execute_at": datetime(2026, 10, 9, 11, 2).timestamp(),
                 "last_stop_check_at": datetime(2026, 10, 9, 13, 1, 45).timestamp(),
                 "active_stage": "scan", "stage_started_at": datetime(2026, 10, 9, 13, 0).timestamp(),
                 "stage_budget_seconds": 480, "last_error": ""}
        with open(state_path, "w") as file:
            json.dump(state, file)
        kwargs = dict(status_override="盘中", trading_day_override=True, state_file=state_path,
                      lock_file=os.path.join(directory, "lock.json"),
                      control_file=os.path.join(directory, "control.json"),
                      db_path=os.path.join(directory, "quant.db"), max_scan_lag_sec=2700,
                      browser_process_probe=lambda: {"available": True, "instance_count": 0})
        items = {x.name: x for x in run_auto_watchdog(now=datetime(2026, 10, 9, 13, 2), **kwargs)}
        assert items["盘中模拟执行"].ok
        assert "1800秒" in items["盘中模拟执行"].detail
        items = {x.name: x for x in run_auto_watchdog(now=datetime(2026, 10, 9, 13, 20), **kwargs)}
        assert items["盘中模拟执行"].severity == "critical"
        assert items["盘中止损巡检"].severity == "critical"


if __name__ == "__main__":
    for name, function in list(globals().items()):
        if name.startswith("test_"):
            function()
            print("OK", name)
