"""Review-to-memory contracts use fractional returns throughout."""
import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from review.llm_review import extract_and_save_lessons


class ReviewLessonRatioTests(unittest.TestCase):
    def test_realistic_loss_and_profit_create_correctly_scaled_lessons(self):
        memory = Mock()
        review = {"trade_reviews": [
            {"code": "600001", "action": "SELL", "result_pct": -0.06},
            {"code": "600002", "action": "SELL", "result_pct": 0.08},
        ]}
        with patch("review.llm_review._analyze_lessons_with_llm", return_value=[]):
            count = extract_and_save_lessons(review, memory=memory)
        self.assertEqual(count, 2)
        calls = [call.kwargs for call in memory.save_lesson.call_args_list]
        self.assertIn("-6.0%", calls[0]["content"])
        self.assertEqual(calls[0]["importance"], 4)
        self.assertIn("+8.0%", calls[1]["content"])

    def test_small_moves_do_not_cross_two_percent_threshold(self):
        memory = Mock()
        review = {"trade_reviews": [
            {"code": "600001", "action": "SELL", "result_pct": -0.02},
            {"code": "600002", "action": "SELL", "result_pct": 0.02},
        ]}
        with patch("review.llm_review._analyze_lessons_with_llm") as analyze:
            self.assertEqual(extract_and_save_lessons(review, memory=memory), 0)
        analyze.assert_not_called()
        memory.save_lesson.assert_not_called()


if __name__ == "__main__":
    unittest.main()
