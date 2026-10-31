"""阶段0：熊市预算死锁、LLM 现价上下文、HOLD 复用与指令护栏的回归。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import pipeline
from strategy import llm_hold_cache
from strategy.decision import DimensionScore
from strategy.directive import (
    MIN_WEIGHT, RELAX_MAX_STEP, marginal_band_evidence, normalize_strategy_directive,
)
from strategy.llm_trader import _build_decision_prompt


class BuyFloorTests(unittest.TestCase):
    def test_floor_is_capped_by_asset_ratio(self):
        self.assertEqual(pipeline.min_buy_amount(100_000), 1500)
        self.assertEqual(pipeline.min_buy_amount(1_000_000), 3000)
        self.assertEqual(pipeline.min_buy_amount(0), 3000)

    def test_bear_and_minimum_directive_budgets_clear_floor(self):
        """熊市 max_weight=0.05 与指令下限 0.03 在 10 万资金下都必须能通过最低金额。"""
        for max_weight in (0.05, MIN_WEIGHT):
            for composite in (50, 62, 75):
                budget = 100_000 * pipeline.target_buy_weight(max_weight, composite)
                self.assertGreaterEqual(budget, pipeline.min_buy_amount(100_000),
                                        f"max_weight={max_weight} composite={composite}")

    def test_target_weight_never_exceeds_cap(self):
        self.assertEqual(pipeline.target_buy_weight(0.1, 150), 0.1)
        self.assertAlmostEqual(pipeline.target_buy_weight(0.1, 60), 0.06)


class QuotePromptTests(unittest.TestCase):
    def prompt(self, quote):
        database = MagicMock()
        database.return_value.__enter__.return_value.conn.execute.return_value.fetchall.return_value = []
        dims = {"technical": DimensionScore("technical", 60, 0.5, "")}
        with patch("data.database.Database", database):
            return _build_decision_prompt("600519", "测试", dims, regime="bear", market_quote=quote)

    def test_prompt_contains_validated_signal_price_and_budget(self):
        text = self.prompt({"price": 10.5, "prev_close": 10.0, "at": "2026-11-02T10:00:00+08:00",
                            "daily_close": 10.0, "max_drift": 0.03, "target_amount": 3100})
        self.assertIn("现价: 10.50", text)
        self.assertIn("+5.00%", text)
        self.assertIn("±3%", text)
        self.assertIn("一手成本约 1,050 元（可买）", text)
        self.assertIn("不得以“缺少现价/可成交价差证据”作为 HOLD 理由", text)

    def test_missing_quote_is_explicit(self):
        self.assertIn("信号时点实时行情不可用", self.prompt(None))

    def test_unreachable_plan_conditions_are_declared_non_binding(self):
        self.assertIn("超出该字段实际取值范围", self.prompt(None))


class HoldReuseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "hold.json")
        self.base = dict(date="2026-11-02", directive_version="v1", regime="bear", state_file=self.path)

    def tearDown(self):
        self.folder.cleanup()

    def remember(self, **kw):
        llm_hold_cache.remember("600519", **{**self.base, "composite": 60, "price": 10.0,
                                             "reason": "等待放量", "confidence": 0.7, "now_ts": 1000, **kw})

    def lookup(self, **kw):
        return llm_hold_cache.lookup("600519", **{**self.base, "composite": 60.5, "price": 10.1,
                                                  "now_ts": 1600, "window_seconds": 3600, **kw})

    def test_same_inputs_reuse_hold(self):
        self.remember()
        self.assertEqual(self.lookup()["reason"], "等待放量")

    def test_material_change_forces_new_llm_call(self):
        self.remember()
        cases = [dict(composite=63), dict(price=10.4), dict(now_ts=1000 + 3601),
                 dict(directive_version="v2"), dict(regime="sideways"), dict(date="2026-11-03")]
        for change in cases:
            self.assertIsNone(self.lookup(**change), change)

    def test_disabled_window_and_corrupt_file_do_not_reuse(self):
        self.remember()
        self.assertIsNone(self.lookup(window_seconds=0))
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        self.assertIsNone(self.lookup())


class DirectiveGuardTests(unittest.TestCase):
    RAW = {"summary": "s", "diagnosis": "d", "rationale": "r", "evaluation": {"verdict": "inconclusive"},
           "params": {"top_k": 3, "min_score": 50, "max_weight": 0.2}}
    CURRENT = {"top_k": 3, "min_score": 62, "max_weight": 0.05}

    def normalize(self, evidence):
        return normalize_strategy_directive(dict(self.RAW), "2026-10-30", "2026-11-02", "bear",
                                            current_params=self.CURRENT, relax_evidence=evidence)["params"]

    def test_inconclusive_without_evidence_stays_locked(self):
        params = self.normalize(None)
        self.assertEqual((params["min_score"], params["max_weight"]), (62, 0.05))

    def test_positive_marginal_evidence_allows_one_small_step_only(self):
        params = self.normalize({"allowed": True, "n": 30, "mean_net_5d": 0.01, "win_rate": 0.55})
        self.assertEqual(params["min_score"], 62 - RELAX_MAX_STEP)
        self.assertEqual(params["max_weight"], 0.05)

    def test_marginal_band_evidence_from_candidate_outcomes(self):
        with tempfile.TemporaryDirectory() as folder:
            db = MagicMock()
            db.conn = sqlite3.connect(os.path.join(folder, "q.db"))
            db.conn.execute("CREATE TABLE candidate_outcomes (observation_date TEXT, observed_at TEXT, "
                            "code TEXT, score REAL, net_return_5d REAL)")
            rows = [(f"2026-10-{d:02d}", f"2026-10-{d:02d}T10", f"{c:06d}", 59.0, -0.02)
                    for d in range(5, 25) for c in range(2)]
            db.conn.executemany("INSERT INTO candidate_outcomes VALUES (?,?,?,?,?)", rows)
            # 同日同股重复观察只计一次
            db.conn.execute("INSERT INTO candidate_outcomes VALUES ('2026-10-05','2026-10-05T11','000000',59,0.5)")
            evidence = marginal_band_evidence(db, 62, "2026-10-30")
            self.assertEqual(evidence["n"], 40)
            self.assertFalse(evidence["allowed"])
            db.conn.execute("UPDATE candidate_outcomes SET net_return_5d = 0.01")
            self.assertTrue(marginal_band_evidence(db, 62, "2026-10-30")["allowed"])
            self.assertFalse(marginal_band_evidence(db, 62, "2026-10-30", min_samples=100)["allowed"])
            db.conn.close()

    def test_missing_table_means_no_relaxation(self):
        db = MagicMock()
        db.conn = sqlite3.connect(":memory:")
        self.assertFalse(marginal_band_evidence(db, 62, "2026-10-30")["allowed"])
        db.conn.close()


if __name__ == "__main__":
    unittest.main()
