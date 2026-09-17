#!/usr/bin/env python3
"""regime_current 状态链路集成测试。

验证 agent_status 构建的状态快照与公网 /api/public/status 端点
都返回带日期和置信度的 regime_current，且键名与前端 app.js 读取一致。
使用临时数据库，不触碰 data/quant.db。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import Database
from fastapi.testclient import TestClient


class RegimeCurrentEndpointTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "regime_test.db")
        self.addCleanup(self._tmp.cleanup)
        # Patch the default path, not the class: delayed imports must never
        # retain a test factory after cleanup. Leave sys.modules untouched.
        database_path = patch("data.database.DB_PATH", self.db_path)
        database_path.start()
        self.addCleanup(database_path.stop)

    def _insert_regime(self, date, regime, confidence):
        with Database(db_path=self.db_path) as db:
            db.insert_market_regime({
                "date": date,
                "regime": regime,
                "confidence": confidence,
                "indicators": json.dumps({"test": 1}),
                "llm_reasoning": "测试环境",
            })

    def test_agent_status_returns_regime_current(self):
        self._insert_regime("2026-09-16", "sideways", 0.82)
        import scheduler.agent_status as agent_status_module
        snapshot = agent_status_module.build_agent_status_snapshot()
        regime = snapshot.get("regime_current")
        self.assertIsNotNone(regime, "快照应包含 regime_current")
        self.assertEqual(regime["regime"], "sideways")
        self.assertEqual(regime["date"], "2026-09-16")
        self.assertAlmostEqual(float(regime["confidence"]), 0.82)
        self.assertEqual(regime["source"], "market_regimes")

    def test_regime_current_missing_is_none(self):
        import scheduler.agent_status as agent_status_module
        snapshot = agent_status_module.build_agent_status_snapshot()
        self.assertIsNone(snapshot.get("regime_current"))

    def test_public_status_endpoint_returns_regime_current(self):
        self._insert_regime("2026-09-16", "bull", 0.9)
        from web.server import app
        client = TestClient(app)
        response = client.get("/api/public/status")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        # 打印一次键名，供与前端读取的键比对
        print("public/status keys:", sorted(payload.keys()))
        regime = payload.get("regime_current")
        self.assertIsNotNone(regime, "公网状态应包含 regime_current")
        self.assertEqual(regime["regime"], "bull")
        self.assertEqual(regime["date"], "2026-09-16")
        self.assertAlmostEqual(float(regime["confidence"]), 0.9)
        # 前端 app.js 读取 data.regime_current；确认键存在且非 adaptive
        self.assertIn("regime_current", payload)
        self.assertIn("adaptive", payload)

    def test_public_status_sanitizes_regime_text(self):
        self._insert_regime("2026-09-16", "sideways", 0.8)
        from web.server import app
        client = TestClient(app)
        payload = client.get("/api/public/status").json()
        regime = payload["regime_current"]
        self.assertTrue(all(isinstance(v, (str, int, float, type(None)))
                            for v in regime.values()))


if __name__ == "__main__":
    unittest.main()
