"""编排审计回归：全部使用临时状态或替身，不调用行情/LLM。"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scheduler import pipeline
from scheduler.control import get_auto_control_state
from scheduler.persistence import update_json


def test_json_cross_process_merge():
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "state.json")
        update_json(path, lambda previous: {"counter": 0})
        code = "from scheduler.persistence import update_json; import sys; [update_json(sys.argv[1], lambda old: {'counter': old['counter'] + 1}) for _ in range(30)]"
        processes = [subprocess.Popen([sys.executable, "-c", code, path], cwd=Path(__file__).resolve().parents[1]) for _ in range(3)]
        try:
            statuses = [process.wait(timeout=30) for process in processes]
            assert statuses == [0, 0, 0]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
        assert json.loads(Path(path).read_text(encoding="utf-8"))["counter"] == 90


def test_corrupt_control_pauses():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "control.json"
        path.write_text("{", encoding="utf-8")
        assert get_auto_control_state(str(path))["paused"]


def test_execution_requires_market_window():
    plan = {"date": "2026-09-28", "orders": []}
    with patch.object(pipeline, "_now_bj", return_value=datetime(2026, 9, 28, 9, 5)), patch.object(pipeline, "is_trading_day", return_value=True):
        result = pipeline.execute_trade_plan(plan, market_status="盘前")
    assert any("盘中" in error for error in result.errors)


def test_review_claim_status_visible():
    class FakeDatabase:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def claim_review_execution(self, date):
            return {"claimed": False, "status": "needs_review"}
    with patch("data.database.Database", FakeDatabase), patch.object(pipeline, "_run_review_impl") as review:
        result = pipeline.run_review()
    assert result.errors and not result.steps[0].success
    review.assert_not_called()


def test_failed_claimed_plan_observed():
    marked = []
    class FakeDatabase:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def mark_trade_plan_needs_review(self, plan_id, error):
            marked.append((plan_id, error))
    def crashed(plan, _execution_context, **kwargs):
        _execution_context.update(plan_id="claimed", db_path="isolated")
        raise RuntimeError("after partial fill")
    with patch("data.database.Database", FakeDatabase), patch.object(pipeline, "_execute_trade_plan_impl", side_effect=crashed):
        result = pipeline.execute_trade_plan({})
    assert marked == [("claimed", "after partial fill")]
    assert result.errors


def test_partial_scores_reported_and_completed_bars_only():
    import pandas as pd
    from strategy.decision import DimensionScore
    frames = []
    daily = pd.DataFrame({"date": ["2026-09-25", "2026-09-28"], "close": [10.0, 100.0]})
    def dimensions(code, frame):
        frames.append(frame)
        return {"technical": DimensionScore("technical", 80, 0.8, "fresh")}
    errors = []
    with patch("data.history.get_daily", return_value=daily), patch("strategy.decision.compute_dimension_scores", side_effect=dimensions), patch("data.eastmoney.cleanup_eastmoney"), patch.object(pipeline, "_now_bj", return_value=datetime(2026, 9, 28, 10)), patch.dict(os.environ, {"TECHNICAL_PATTERN_SHADOW": "0"}):
        results = pipeline._parallel_score([{"code": "600519"}], {}, errors=errors)
    assert len(results) == 1 and results[0]["latest_price"] == 10
    assert frames[0]["date"].tolist() == ["2026-09-25"]
    stale = daily.copy()
    stale.attrs["coverage_status"] = "stale"
    with patch("data.history.get_daily", return_value=stale), patch("data.eastmoney.cleanup_eastmoney"):
        results = pipeline._parallel_score([{"code": "600519"}], {}, errors=errors)
    assert not results and any("陈旧" in error for error in errors)


def test_sensor_event_bus_cross_loop_start():
    from realtime.event_bus import EventBus, Event
    bus = EventBus()
    bus.publish_sync(Event("sample", {"value": 1}))
    asyncio.run(bus.publish(Event("sample", {"value": 2})))
    received = []
    async def handler(event):
        received.append(event.data["value"])
        if event.data["value"] == 2:
            await asyncio.to_thread(bus.stop)
            await asyncio.sleep(0)
    bus.subscribe("sample", handler)
    asyncio.run(bus.start())
    assert received == [1, 2]
    bus.publish_sync(Event("sample", {"value": 2}))
    asyncio.run(bus.start())
    assert received == [1, 2, 2]


def test_stop_check_cli_failure_exit():
    import main
    with patch.object(main, "cmd_stop_check", return_value={"errors": ["报价过期"]}), \
            patch.object(sys, "argv", ["main.py", "--stop-check"]):
        try:
            main.main()
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("止损巡检失败必须返回非零退出码")


def test_failed_stop_stage_does_not_claim_completion():
    from scheduler.auto_trader import AutoTraderState, run_auto_cycle
    from datetime import datetime
    state = AutoTraderState(date="2026-09-28")
    result = run_auto_cycle(
        state=state, status_override="盘中", trading_day_override=True,
        today_override="2026-09-28", now_override=datetime(2026, 9, 28, 10),
        now_ts_override=1000, persist_state=False, record_event=False, notify=False,
        services={"check_stops_once": lambda: {"checked": 1, "sold": 0, "trades": [], "error": "报价过期"}},
    )
    assert result["state"]["last_stop_check_at"] == 0
    assert "报价过期" in result["state"]["last_error"]


if __name__ == "__main__":
    for name, function in list(globals().items()):
        if name.startswith("test_"):
            function()
            print("OK", name)
