"""复盘失败不重放、不刷屏，成功修复后可以同步完成状态。"""
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.database import Database
from scheduler.auto_trader import AutoTraderState, run_auto_cycle, run_auto_loop


def test_review_error_not_replayed_or_repeated_after_restart():
    with tempfile.TemporaryDirectory() as directory:
        db_path = os.path.join(directory, "quant.db")
        state_file = os.path.join(directory, "state.json")
        with Database(db_path=db_path) as db:
            db.claim_review_execution("2026-10-09")
        calls = []; notices = []

        def review():
            calls.append(True)
            return SimpleNamespace(steps=[], errors=["今日复盘未完成，领取状态=executing，需核对"])

        def notify(**kwargs):
            notices.append(kwargs)
            return True

        kwargs = dict(status_override="盘后", trading_day_override=True, today_override="2026-10-09",
                      now_override=datetime(2026, 10, 9, 16), after_review_time_override=True,
                      services={"run_review": review, "send_auto_cycle_report": notify},
                      db_path=db_path, state_file=state_file, persist_state=True, record_event=True, notify=True)
        result = run_auto_cycle(state=AutoTraderState(date="2026-10-09"), **kwargs)
        assert len(notices) == 1 and result["state"]["last_review_date"] == ""
        with open(state_file, encoding="utf-8") as file:
            restarted = AutoTraderState(**json.load(file))
        result = run_auto_cycle(state=restarted, **kwargs)
        assert len(calls) == 1 and len(notices) == 1
        assert result["state"]["last_error"]
        with Database(db_path=db_path) as db:
            failed = db.conn.execute("SELECT status,error FROM auto_events WHERE event_type='stage_result'").fetchone()
            assert failed["status"] == "failed" and failed["error"]
            db.complete_review_execution("2026-10-09")
        result = run_auto_cycle(state=AutoTraderState(**result["state"]), **kwargs)
        assert result["state"]["last_review_date"] == "2026-10-09"
        assert not result["state"]["last_error"] and len(calls) == 1


def test_failed_notification_is_retried_without_replaying_review():
    with tempfile.TemporaryDirectory() as directory:
        db_path = os.path.join(directory, "quant.db")
        with Database(db_path=db_path) as db:
            db.claim_review_execution("2026-10-09")
        notices = []
        def notify(**kwargs):
            notices.append(True)
            return len(notices) > 1
        kwargs = dict(status_override="盘后", trading_day_override=True, today_override="2026-10-09",
                      now_override=datetime(2026, 10, 9, 16), after_review_time_override=True,
                      services={"send_auto_cycle_report": notify}, db_path=db_path,
                      persist_state=False, record_event=False, notify=True)
        state = AutoTraderState(date="2026-10-09", last_review_attempt_date="2026-10-09")
        for _ in range(3):
            result = run_auto_cycle(state=state, **kwargs)
            state = AutoTraderState(**result["state"])
        assert len(notices) == 2


def test_deploy_hold_ack_before_next_cycle():
    import scheduler.auto_trader as module
    with tempfile.TemporaryDirectory() as directory:
        hold = os.path.join(directory, "hold.json")
        with open(hold, "w") as file:
            json.dump({"nonce": "test-deploy", "expires_at": time.time() + 30}, file)
        calls = []; errors = []
        def deploy():
            try:
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    if os.path.exists(hold + ".ready"):
                        with open(hold + ".ready") as file:
                            ready = json.load(file)
                        assert ready["nonce"] == "test-deploy" and not calls
                        os.unlink(hold)
                        return
                    time.sleep(.01)
                raise AssertionError("未确认部署安全边界")
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=deploy);worker.start()
        with patch.object(module, "AUTO_DEPLOY_HOLD_FILE", hold), \
                patch.object(module, "_load_state", return_value=AutoTraderState(date="2026-10-09")), \
                patch.object(module, "run_auto_cycle", side_effect=lambda **kwargs: calls.append(True) or {"state": vars(kwargs["state"])}):
            run_auto_loop(once=True)
        worker.join(5)
        assert not errors and calls == [True]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("OK", name)
