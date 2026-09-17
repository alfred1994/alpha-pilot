"""Offline SQLite evidence and public pagination contracts."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from strategy.memory import TradeMemory
from web.routers import database as router


class DecisionEvidenceTest(unittest.TestCase):
    def test_memory_persists_snapshot_and_public_api_uses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.db")
            with TradeMemory(db_path=path) as memory:
                first = memory.save_decision(
                    code="000001", action="HOLD", prompt="PRIVATE_PROMPT",
                    response="PRIVATE_RESPONSE", reasoning="等待证据", confidence=0.7,
                    dimensions={"ml": {"score": 60, "confidence": 0.8, "detail": "PRIVATE_DETAIL"}},
                    scan_id="scan-one",
                )
                second = memory.save_decision(
                    code="000001", action="BUY", prompt="PRIVATE_PROMPT",
                    response="PRIVATE_RESPONSE", reasoning="当前证据充分", confidence=0.8,
                    dimensions={"ml": {"score": 90, "confidence": 0.9}}, scan_id="scan-two",
                )
            self.assertGreater(first, 0)
            self.assertGreater(second, first)
            # Reopen to exercise idempotent schema initialization and real persistence.
            with Database(db_path=path) as db:
                row = dict(db.conn.execute("SELECT * FROM llm_decisions WHERE id=?", (first,)).fetchone())
                self.assertEqual(json.loads(row["dimensions"])["ml"]["score"], 60)
                self.assertEqual(row["scan_id"], "scan-one")
                db.insert_llm_decision({"code": "000002", "date": "2000-01-01", "action": "HOLD", "reasoning": "legacy"})
            with patch.object(router, "_get_db", side_effect=lambda: Database(db_path=path)):
                newest = router.get_decisions(limit=1, page=1)
                previous = router.get_decisions(limit=1, page=2)
                self.assertEqual(newest["total"], 3)
                self.assertTrue(newest["has_more"])
                self.assertEqual(newest["decisions"][0]["dimensions"]["ml"]["score"], 90)
                self.assertEqual(previous["decisions"][0]["dimensions"]["ml"]["score"], 60)
                self.assertEqual(previous["decisions"][0]["scan_id"], "scan-one")
                self.assertNotIn("PRIVATE_", json.dumps(previous))
                signals = router.get_decisions(limit=10, kind="signal")
                self.assertEqual(signals["total"], 1)
                legacy = router.get_decisions(limit=10, start_date="2000-01-01", end_date="2000-01-01")
                self.assertEqual(legacy["total"], 1)
                self.assertEqual(legacy["decisions"][0]["dimensions"], {})
                self.assertFalse(legacy["decisions"][0]["evidence_available"])
                self.assertFalse(router.get_decisions(limit=10, page=0)["success"])
                self.assertFalse(router.get_decisions(limit=10, start_date="invalid")["success"])


if __name__ == "__main__":
    unittest.main()
