"""复盘角色不得重复计算交易、误判退出或重写代码统计。"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.database import Database
from strategy.prompt_evolution import analyze_decision_accuracy, format_evolution_report


class PromptEvolutionAuditTest(unittest.TestCase):
    def test_unique_buys_window_and_reviewer_cannot_override_metrics(self):
        today = datetime.now().strftime("%Y-%m-%d")
        old = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as folder, Database(db_path=os.path.join(folder, "test.db")) as db:
            trade_id = db.insert_trade({"code": "000001", "action": "BUY", "price": 10,
                                       "shares": 100, "created_at": today + "T09:40:00"})
            for action, day, outcome, value in [
                ("BUY", today, "risk_exit", -3), ("BUY", today, "risk_exit", -3),
                ("SELL", today, "lose", -3), ("BUY", old, "win", 8),
            ]:
                db.insert_llm_decision({"code": "000001", "date": day, "action": action,
                    "trade_id": trade_id, "outcome": outcome, "outcome_pct": value,
                    "created_at": day + "T09:30:00", "confidence": 0.6})
            with patch("strategy.prompt_evolution._call_llm", return_value=json.dumps({
                "total_decisions": 100, "win_rate": 1, "prompt_suggestions": ["跟踪突破失效后是否回落，三日后复核"]})) as reviewer:
                result = analyze_decision_accuracy(db, days=7)
            self.assertEqual(result["total_decisions"], 1)
            self.assertEqual(result["lose_count"], 1)
            self.assertEqual(result["win_rate"], 0)
            self.assertEqual(len(result["prompt_suggestions"]), 1)
            self.assertIn("risk_exit", reviewer.call_args.args[0])

    def test_no_mature_buys_still_reviews_hold_and_marks_profit_ratio_unknown(self):
        today = datetime.now().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as folder, Database(db_path=os.path.join(folder, "test.db")) as db:
            db.insert_llm_decision({"code": "000001", "date": today, "action": "HOLD",
                                    "confidence": 0.6, "created_at": today + "T10:00:00"})
            with patch("strategy.prompt_evolution._call_llm", return_value='{}') as reviewer:
                result = analyze_decision_accuracy(db)
            reviewer.assert_called_once()
            self.assertFalse(result["win_rate_available"])
            self.assertEqual(result["hold_opportunity_audit"]["n_flat_hold_stock_days"], 1)
            self.assertIn("未知", format_evolution_report(result))


if __name__ == "__main__":
    unittest.main()
