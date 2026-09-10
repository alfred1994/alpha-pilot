"""策略防护回归：可转债冷却、一手预算、T+1锁定、复盘清洗与教训去重。"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 本机缺少行情/HTTP依赖时，用轻量 mock 保证纯函数单测可运行
for _mod in (
    "requests", "akshare", "baostock", "pandas", "numpy",
    "schedule", "flask", "fastapi", "uvicorn",
):
    sys.modules.setdefault(_mod, MagicMock())


class TestCbStopOutCooldown(unittest.TestCase):
    def setUp(self):
        from strategy import cb_t0_strategy as cb
        self.cb = cb
        with cb._STOP_OUT_LOCK:
            cb._STOPPED_OUT_TODAY["date"] = ""
            cb._STOPPED_OUT_TODAY["codes"] = set()

    def tearDown(self):
        with self.cb._STOP_OUT_LOCK:
            self.cb._STOPPED_OUT_TODAY["date"] = ""
            self.cb._STOPPED_OUT_TODAY["codes"] = set()

    def test_mark_blocks_rebuy(self):
        cb = {
            "cb_code": "123112",
            "cb_name": "万讯转债",
            "stock_code": "300112",
            "total_score": 85,
            "premium_rate": 2.0,
            "stock_change_pct": 8.0,
            "cb_price": 150.0,
        }
        self.assertTrue(self.cb.should_buy(cb)["buy"])
        self.cb.mark_stopped_out("123112")
        decision = self.cb.should_buy(cb)
        self.assertFalse(decision["buy"])
        self.assertIn("止损冷却", decision["reason"])

    def test_scan_filters_stopped_codes(self):
        self.cb.mark_stopped_out("123112")
        self.assertTrue(self.cb.is_stop_out_blocked("123112"))
        self.assertFalse(self.cb.is_stop_out_blocked("128000"))


class TestMinLotAffordable(unittest.TestCase):
    def test_high_price_small_weight_blocked(self):
        from scheduler.pipeline import min_lot_affordable
        self.assertFalse(
            min_lot_affordable(
                price=340.0,
                target_weight=0.05,
                total_assets=1_000_000,
                position_scale=0.5,
                trade_unit=100,
            )
        )

    def test_low_price_affordable(self):
        from scheduler.pipeline import min_lot_affordable
        self.assertTrue(
            min_lot_affordable(
                price=11.0,
                target_weight=0.05,
                total_assets=1_000_000,
                position_scale=0.5,
                trade_unit=100,
            )
        )

    def test_missing_data_allows(self):
        from scheduler.pipeline import min_lot_affordable
        self.assertTrue(min_lot_affordable(price=0, target_weight=0.05, total_assets=1_000_000))
        self.assertTrue(min_lot_affordable(price=340, target_weight=0.05, total_assets=0))


class TestT1Lock(unittest.TestCase):
    def test_is_t1_locked(self):
        from strategy.llm_trader import is_t1_locked
        locked = {"buy_date": "2026-09-10", "shares": 100}
        self.assertTrue(is_t1_locked(locked, today="2026-09-10"))
        self.assertFalse(is_t1_locked(locked, today="2026-09-11"))
        self.assertFalse(is_t1_locked({**locked, "allow_t0": True}, today="2026-09-10"))
        self.assertFalse(is_t1_locked({"buy_date": ""}, today="2026-09-10"))


class TestReviewTextClean(unittest.TestCase):
    def test_strips_politeness(self):
        from review.llm_review import clean_llm_review_text
        raw = "好的，这是基于您提供的交易数据和市场信息的专业复盘分析。\n### 1. 当日战况总结\n今日亏损。"
        cleaned = clean_llm_review_text(raw)
        self.assertTrue(cleaned.startswith("### 1. 当日战况总结"))
        self.assertNotIn("好的", cleaned[:10])
        self.assertNotIn("基于您提供", cleaned)

    def test_json_fragment_strip(self):
        from strategy.llm_trader import _strip_json_fragments
        messy = 'LLM卖出: 从文本推断: {"action": "HOLD", "confidence": 0.65}'
        cleaned = _strip_json_fragments(messy)
        self.assertNotIn("{", cleaned)
        self.assertIn("从文本推断", cleaned)


class TestLessonDedup(unittest.TestCase):
    def test_normalize_key_collapses_numbers(self):
        from review.llm_review import _normalize_lesson_key
        a = _normalize_lesson_key("⚠️ 胜率极低(<30%)，建议暂停交易1-2天")
        b = _normalize_lesson_key("⚠️ 胜率极低(<25%)，建议暂停交易3-4天")
        self.assertEqual(a, b)

    def test_save_dedup_skips_recent_duplicate(self):
        from review.llm_review import _save_lesson_dedup
        memory = MagicMock()
        conn = MagicMock()
        memory._get_db.return_value.conn = conn
        content = "⚠️ 胜率极低(<30%)，建议暂停交易1-2天，检查信号逻辑"
        conn.execute.return_value.fetchall.return_value = [{"content": content}]
        self.assertFalse(_save_lesson_dedup(memory, "general", content))
        memory.save_lesson.assert_not_called()

    def test_save_dedup_writes_when_new(self):
        from review.llm_review import _save_lesson_dedup
        memory = MagicMock()
        conn = MagicMock()
        memory._get_db.return_value.conn = conn
        conn.execute.return_value.fetchall.return_value = []
        self.assertTrue(_save_lesson_dedup(memory, "general", "全新教训内容"))
        memory.save_lesson.assert_called_once()


if __name__ == "__main__":
    unittest.main()
