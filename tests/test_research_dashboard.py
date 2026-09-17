"""Offline HTTP contracts for the read-only research dashboard."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from data.database import Database
from web.routers import research


class ResearchDashboardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "research.db")
        self.db_patch = patch("data.database.DB_PATH", self.path)
        self.db_patch.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.db_patch.stop)
        app = FastAPI()
        app.include_router(research.router, prefix="/api")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def seed(self):
        with Database(db_path=self.path) as db:
            db.conn.execute("INSERT INTO market_regimes (date,regime,indicators,llm_reasoning) VALUES (?,?,?,?)",
                            ("2020-01-01", "bull", json.dumps({"limit_up_count": 0,
                             "hs300_pct_5d": 3, "trend_data_stale_days": 4,
                             "secret": "PRIVATE_INDICATOR"}), "PRIVATE_PROMPT"))
            db.conn.commit()
            for i in range(3):
                db.insert_candidate_outcome({
                    "observation_key": f"obs-{i}", "scan_id": f"scan-{i}",
                    "observation_date": "2020-01-01", "observed_at": f"2020-01-01T10:0{i}:00",
                    "code": "000001", "name": "测试股票", "action": "HOLD",
                    "llm_action": "HOLD", "entry_price": 10,
                    "denial_layer": "llm", "hold_reason": "token=abc /home/private/key",
                    "dimensions": json.dumps({"ml": {"score": 60, "confidence": 0.8,
                                                     "detail": "PRIVATE_DETAIL"}}),
                })
            db.conn.execute("UPDATE candidate_outcomes SET net_return_5d=0 WHERE observation_key='obs-0'")
            db.conn.execute("UPDATE candidate_outcomes SET net_return_5d=0.1 WHERE observation_key='obs-1'")
            db.conn.commit()

    def test_absent_db_is_not_created(self):
        for endpoint in ("market", "candidates"):
            result = self.client.get(f"/api/research/{endpoint}").json()
            self.assertTrue(result["success"])
            self.assertFalse(result["available"])
        self.assertFalse(Path(self.path).exists())

    def test_market_whitelist_stale_and_zero(self):
        self.seed()
        result = self.client.get("/api/research/market").json()
        self.assertTrue(result["success"])
        market = result["market"]
        self.assertTrue(market["stale"])
        metrics = {v["key"]: v["value"] for v in market["metrics"]}
        self.assertEqual(metrics["limit_up_count"], 0)
        self.assertIsNone(metrics["limit_down_count"])
        self.assertIsNone(metrics["hs300_pct_5d"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_pagination_maturity_duplicates_redaction_and_read_only(self):
        self.seed()
        before = Path(self.path).read_bytes()
        url = "/api/research/candidates?start_date=2020-01-01&end_date=2020-01-01&limit=1"
        first = self.client.get(url).json()
        second = self.client.get(url + "&page=2").json()
        self.assertTrue(first["success"])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["summary"]["observations"], 3)
        self.assertEqual(first["summary"]["unique_stocks"], 1)
        self.assertEqual(first["summary"]["matured_5d"], 2)
        self.assertAlmostEqual(first["groups"][0]["positive_rate_5d"], 0.5)
        self.assertAlmostEqual(first["groups"][0]["mean_net_5d"], 0.05)
        self.assertIsNone(first["candidates"][0]["net_return_5d"])
        self.assertNotEqual(first["candidates"][0]["id"], second["candidates"][0]["id"])
        self.assertEqual(first["candidates"][0]["dimensions"]["ml"]["score"], 60)
        for private in ("PRIVATE", "token=abc", "/home/private"):
            self.assertNotIn(private, json.dumps(first))
        empty = self.client.get(url + "&layer=buy_budget").json()
        self.assertEqual(empty["summary"]["observations"], 0)
        self.assertEqual(empty["candidates"], [])
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_invalid_filters(self):
        for query in ("page=0", "limit=51", "start_date=bad", "layer=sql",
                      "start_date=2020-02-01&end_date=2020-01-01",
                      "start_date=2000-01-01&end_date=2020-01-01"):
            self.assertEqual(self.client.get("/api/research/candidates?" + query).status_code, 422)

    def test_database_failure_redacted(self):
        with patch.object(research, "_open_db", side_effect=RuntimeError("/home/private token=SECRET")):
            for endpoint in ("market", "candidates"):
                data = self.client.get(f"/api/research/{endpoint}").json()
                self.assertFalse(data["success"])
                self.assertNotIn("SECRET", str(data))


if __name__ == "__main__":
    unittest.main()
