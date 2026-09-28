"""看板只读、失败状态、快照与公开脱敏回归。"""
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from web.snapshot_cache import SnapshotCache
from web.public_safety import sanitize_status_snapshot


def test_cache_singleflight_and_backoff():
    cache = SnapshotCache(ttl=1, failure_backoff=0.2)
    counter = []
    def build():
        counter.append(1)
        time.sleep(0.05)
        return {"value": len(counter)}
    with ThreadPoolExecutor(max_workers=12) as pool:
        values = list(pool.map(lambda _: cache.get("status", build), range(12)))
    assert len(counter) == 1 and all(value["value"] == 1 for value in values)
    assert len({value["snapshot_at"] for value in values}) == 1
    failed = []
    def fail():
        failed.append(1)
        raise ValueError("internal path C:/secret")
    for _ in range(2):
        try:
            cache.get("broken", fail)
        except (ValueError, RuntimeError):
            pass
        else:
            raise AssertionError("失败不得伪装成功")
    assert len(failed) == 1


def test_public_projection_preserves_unknown_and_redacts():
    payload = sanitize_status_snapshot({
        "account": {"available": False},
        "adaptive": {"weights": {"technical": 0.35, "secret": "token=leak"}, "extra": "C:/secret"},
        "daily_trader": {"funnel": {"scored": None, "llm_evaluated": 0}},
        "execution_reviews": {"plans": [{"error": "C:/secret"}], "reviews": []},
    })
    assert payload["daily_trader"]["funnel"]["scored"] is None
    assert payload["daily_trader"]["funnel"]["llm_evaluated"] == 0
    assert payload["execution_reviews"] == {"plans": 1, "reviews": 0, "requires_attention": True}
    assert "secret" not in str(payload["adaptive"])
    assert "C:/secret" not in str(payload)


def test_missing_db_get_is_503_and_never_created():
    import data.database as database_module
    from web.server import app
    with tempfile.TemporaryDirectory() as directory:
        absent = Path(directory) / "missing.db"
        with patch.object(database_module, "DB_PATH", str(absent)):
            response = TestClient(app).get("/api/trades")
        assert response.status_code == 503
        assert response.json()["success"] is False
        assert not absent.exists()


def test_decision_input_is_422():
    from web.server import app
    client = TestClient(app)
    assert client.get("/api/decisions?page=1001").status_code == 422
    assert client.get("/api/decisions?start_date=not-a-date").status_code == 422


def test_status_positions_shadow_do_not_open_writable_database():
    import data.database as database_module
    import execution.paper_account as paper_account_module
    from web.server import app
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "dashboard.db")
        with database_module.Database(db_path=path):
            pass
        original_enter = database_module.Database.__enter__
        opened = []
        def guarded_enter(self):
            opened.append(self.readonly)
            if not self.readonly:
                raise AssertionError("看板GET调用了可写Database")
            return original_enter(self)
        class FakeAccount:
            def __init__(self, *, read_only=False):
                assert read_only
                self.db_path = path
                self.positions = {}
                self.cash = 1000.0
                self.initial_capital = 1000.0
                self.updated_at = None
            def total_assets(self, prices=None):
                return 1000.0
        with patch.object(database_module, "DB_PATH", path), \
                patch.object(database_module.Database, "__enter__", guarded_enter), \
                patch.object(paper_account_module, "PaperAccount", FakeAccount), \
                patch("scheduler.market_calendar.is_trading_day", return_value=True), \
                patch("scheduler.watchdog.is_trading_day", return_value=True), \
                patch("scheduler.trader_brief.is_trading_day", return_value=True), \
                patch("data.realtime.get_realtime", return_value=[]):
            client = TestClient(app)
            assert client.get("/api/public/status").status_code == 200
            assert client.get("/api/positions").status_code == 200
            shadow = client.get("/api/shadow/leaderboard")
            assert shadow.status_code == 200 and shadow.json()["available"] is False
        assert opened and all(opened)


if __name__ == "__main__":
    for name, function in list(globals().items()):
        if name.startswith("test_"):
            function()
            print("OK", name)
