#!/usr/bin/env python3
"""验证 make_decision 将参与评分的 ML 证据传给模型，不访问外部服务。"""
from dataclasses import asdict
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.decision import DimensionScore
from strategy.llm_trader import make_decision


class LLMTraderMLPromptTest(unittest.TestCase):
    def test_make_decision_passes_ml_evidence_to_model(self):
        dimensions = {
            key: DimensionScore(key, 40.0, 0.5, "固定测试证据")
            for key in ("technical", "capital", "sentiment", "emotion", "fundamental")
        }
        dimensions["ml"] = DimensionScore("ml", 90.3, 0.61, "ML_EVIDENCE_SENTINEL")
        memory = MagicMock()
        memory.save_decision.return_value = 123
        database = MagicMock()
        database.return_value.__enter__.return_value.conn.execute.return_value.fetchall.return_value = []

        def model_response(prompt, **kwargs):
            self.assertIn("机器学习", prompt)
            self.assertIn("90分 (置信度61%) ML_EVIDENCE_SENTINEL", prompt)
            return '{"action":"BUY","confidence":0.7,"reasoning":"固定测试买入结论"}'

        with patch("strategy.llm_trader.DEEPSEEK_API_KEY", ""), \
                patch("strategy.llm_trader._call_llm", side_effect=model_response) as model, \
                patch("strategy.llm_trader._call_deepseek") as alternate_model, \
                patch("data.overnight_snapshot.load_overnight_snapshot", return_value=None), \
                patch("data.database.Database", database):
            decision = make_decision(
                code="000001", dimensions=dimensions, memory_context="固定测试记忆",
                current_positions={}, total_assets=1_000_000, cash=1_000_000,
                memory=memory, llm_retries=0, scan_id="test-scan-1",
            )

        model.assert_called_once()
        alternate_model.assert_not_called()
        self.assertEqual(decision.action, "BUY")
        self.assertEqual(decision.decision_id, 123)
        memory.save_decision.assert_called_once()
        saved = memory.save_decision.call_args.kwargs
        self.assertIn("ML_EVIDENCE_SENTINEL", saved["prompt"])
        self.assertEqual(saved["action"], "BUY")
        self.assertEqual(saved["scan_id"], "test-scan-1")
        self.assertEqual(saved["dimensions"]["ml"]["score"], 90.3)
        self.assertEqual(asdict(decision)["scan_id"], "test-scan-1")


if __name__ == "__main__":
    unittest.main()
