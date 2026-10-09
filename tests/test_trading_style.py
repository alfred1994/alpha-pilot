"""动态提示计划的期限、市场切换、制度边界和持久化回归。"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from contextlib import ExitStack
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.database import Database
from strategy.directive import generate_and_save_strategy_directive, get_effective_trade_policy
from strategy.llm_trader import _build_decision_prompt
from strategy.memory import TradeMemory
from strategy.trading_style import build_style_context, normalize_style_plan


def policy(regime="bull", effective="2026-10-09"):
    return {"regime": regime, "effective_date": effective,
            "style_plan": {"preferred_styles": ["event_driven", "intraday_t0"],
                           "focus": "核验公告催化与量价确认", "invalidate_when": "公告被澄清或放量突破失败"}}


class TradingStyleTest(unittest.TestCase):
    def test_scan_keeps_trend_and_low_position_candidates(self):
        from scheduler.pipeline import fast_scan
        from strategy.stock_picker import Candidate
        with ExitStack() as stack:
            for source in ("limit_up", "north_flow", "performance", "survey", "financing", "unlock_alert"):
                stack.enter_context(patch(f"strategy.stock_picker._get_{source}_candidates", return_value={}))
            stack.enter_context(patch("strategy.stock_picker._get_volume_candidates", return_value={
                "300001": Candidate("300001", "趋势样本", ["异动放量"], 80)}))
            stack.enter_context(patch("strategy.stock_picker._get_dragon_tiger_candidates", return_value={
                "000002": Candidate("000002", "事件样本", ["龙虎榜"], 75)}))
            stack.enter_context(patch("strategy.low_position_picker.pick_low_position_stocks", return_value=[
                Candidate("000003", "低位样本", ["低位"], 70)]))
            stack.enter_context(patch("strategy.stock_picker.get_sentiment_boost", return_value={}))
            stack.enter_context(patch("data.realtime.get_realtime_batch", return_value=[]))
            stack.enter_context(patch("data.snapshot.get_candidate_pool_status", return_value={}))
            stack.enter_context(patch("data.snapshot.with_timeout", side_effect=lambda func, **kw: func()))
            saved = stack.enter_context(patch("data.snapshot.save_candidate_pool", return_value={}))
            fast_scan(budget_seconds=5, persist_plan=False)
        self.assertEqual({c.code for c in saved.call_args.args[0]}, {"300001", "000002", "000003"})

    def test_regime_and_date_change_invalidate_review_focus(self):
        current = json.loads(build_style_context("bull", policy(), "2026-10-09"))
        self.assertEqual(current["preferred_styles"], ["event_driven"])
        self.assertIn("独立复盘", current["source"])
        for regime, date in [("bear", "2026-10-09"), ("bull", "2026-10-12")]:
            changed = json.loads(build_style_context(regime, policy(), date))
            self.assertIn("默认", changed["source"])
            self.assertNotEqual(changed["review_focus"], current["review_focus"])

    def test_t0_requires_execution_metadata(self):
        blocked = json.loads(build_style_context("bull", policy(), "2026-10-09"))
        allowed = json.loads(build_style_context("bull", policy(), "2026-10-09", allow_t0=True))
        self.assertNotIn("intraday_t0", blocked["available_styles"])
        self.assertIn("intraday_t0", allowed["preferred_styles"])

    def test_invalid_style_rejected_and_old_directive_compatible(self):
        self.assertIsNone(normalize_style_plan(None))
        for styles in [["unknown"], ["swing", "swing"], [], "swing", [{}]]:
            with self.assertRaises(ValueError):
                normalize_style_plan({**policy()["style_plan"], "preferred_styles": styles})

    def test_critic_plan_persisted_and_consumed_without_relaxing_guards(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "test.db")
            raw = {**policy(), "summary": "主动识别趋势", "diagnosis": "部分短线踏空",
                   "rationale": "样本有限，保持风险预算", "evaluation": {"verdict": "inconclusive"},
                   "params": {"top_k": 3, "min_score": 45, "max_weight": 0.25}}
            saved = generate_and_save_strategy_directive(
                "2026-10-08", {}, "复盘材料", "bull",
                {"top_k": 3, "min_score": 60, "max_weight": 0.06}, db_path=path,
                effective_date="2026-10-09", llm_call=lambda prompt: json.dumps(raw))
            loaded = get_effective_trade_policy("2026-10-09", "bull", db_path=path)
            self.assertEqual(loaded["style_plan"], saved["style_plan"])
            self.assertEqual(loaded["params"]["min_score"], 60)
            self.assertEqual(loaded["params"]["max_weight"], 0.06)

    def test_prompt_has_explicit_time_and_full_persistence(self):
        database = MagicMock()
        database.return_value.__enter__.return_value.conn.execute.return_value.fetchall.return_value = []
        with patch("data.database.Database", database):
            prompt = _build_decision_prompt("000001", "测试", {}, regime="bull",
                current_positions={"000001": {"buy_date": "2026-09-22", "shares": 100, "buy_price": 10}},
                memory_context="测试记忆" * 400)
        self.assertIn(datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d"), prompt)
        self.assertIn("买入日期: 2026-09-22", prompt)
        self.assertIn("trend_following", prompt)
        self.assertIn("120字以内", prompt)
        self.assertGreater(len(prompt), 2000)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "test.db")
            with TradeMemory(db_path=path) as memory:
                identifier = memory.save_decision("000001", "HOLD", prompt, '{"action":"HOLD"}', "等待突破")
            with Database(db_path=path, readonly=True) as db:
                actual = db.conn.execute("SELECT llm_prompt FROM llm_decisions WHERE id=?", (identifier,)).fetchone()[0]
            self.assertEqual(actual, prompt)


if __name__ == "__main__":
    unittest.main()
