"""跨日闭环、有限重试、事实缺口及信号时点价格回归。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.database import Database
from scheduler.review_recovery import recover_pending_directives
from scheduler.trader_brief import build_daily_facts
from scheduler.watchdog import run_auto_watchdog
from scheduler.pipeline import _refresh_signal_prices
from strategy.directive import _call_directive_llm, get_effective_trade_policy


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "quant.db")
        self.now = datetime(2026, 10, 15, 1)
        with Database(db_path=self.db) as db:
            db.claim_review_execution("2026-10-14")
            db.complete_review_execution("2026-10-14", "AI明日策略指令未完成: LLM策略指令无效，沿用上一有效版本")
            db.save_review_snapshot("2026-10-14", {"llm_review": "旧文字复盘", "total_assets": 100000})

    def recovery(self, call, now=None):
        with patch("scheduler.review_recovery._next_trading_date", return_value="2026-10-15"), \
                patch("scheduler.trader_brief.build_daily_facts", return_value={"llm_no_response_count": 1}), \
                patch("review.daily_review._compute_benchmark_pnl_pct", return_value=None):
            return recover_pending_directives(db_path=self.db, now=now or self.now, llm_call=call)

    def test_existing_schema_is_migrated_without_losing_review_claim(self):
        with Database(db_path=self.db) as db:
            db.conn.execute("DROP TABLE review_directive_recovery")
            db.conn.execute("PRAGMA user_version=1")
        with Database(db_path=self.db) as db:
            self.assertEqual(db.conn.execute("PRAGMA user_version").fetchone()[0], Database.SCHEMA_VERSION)
            self.assertEqual(db.conn.execute("SELECT count(*) FROM review_directive_recovery").fetchone()[0], 0)
            self.assertEqual(db.conn.execute("SELECT status FROM review_executions").fetchone()[0], "needs_review")

    def test_unresolved_review_remains_critical_after_midnight_and_weekend(self):
        for now in (self.now, datetime(2026, 10, 17, 1)):
            items = run_auto_watchdog(now=now, status_override="盘前", trading_day_override=False,
                                      db_path=self.db, state_file=str(Path(self.temp.name)/"state.json"),
                                      lock_file=str(Path(self.temp.name)/"lock"),
                                      control_file=str(Path(self.temp.name)/"control.json"))
            item = next(i for i in items if i.name == "未完成复盘")
            self.assertEqual(item.severity, "critical")
            self.assertIn("2026-10-14", item.detail)

    def test_only_directive_is_recovered_once(self):
        calls = []
        def call(prompt):
            calls.append(prompt)
            return json.dumps({"summary": "维持", "diagnosis": "数据降级", "rationale": "证据不足",
                               "evaluation": {"verdict": "inconclusive"},
                               "params": {"top_k": 3, "min_score": 75, "max_weight": .03}})
        result = self.recovery(call)
        self.assertEqual(result[0]["status"], "completed")
        self.assertEqual(self.recovery(call), [])
        self.assertEqual(len(calls), 1)
        with Database(db_path=self.db) as db:
            snapshot = db.get_review_snapshot("2026-10-14")
            self.assertEqual(snapshot["llm_review"], "旧文字复盘")
            self.assertIsNone(snapshot["benchmark_pnl_pct"])
            self.assertEqual(db.conn.execute("SELECT count(*) FROM trades").fetchone()[0], 0)
            self.assertEqual(db.conn.execute("SELECT count(*) FROM lessons").fetchone()[0], 0)
        self.assertTrue(get_effective_trade_policy("2026-10-15", "sideways", self.db)["valid_for_date"])
        self.assertFalse(get_effective_trade_policy("2026-10-16", "sideways", self.db)["valid_for_date"])

    def test_retries_bounded_and_not_intraday(self):
        calls = []
        call = lambda prompt: calls.append(prompt) or "invalid JSON"
        self.recovery(call)
        self.recovery(call, self.now + timedelta(minutes=1))
        self.assertEqual(len(calls), 1)
        self.recovery(call, self.now + timedelta(minutes=16))
        self.recovery(call, self.now + timedelta(minutes=32))
        self.recovery(call, self.now + timedelta(minutes=48))
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.recovery(call, datetime(2026, 10, 15, 10)), [])
        with Database(db_path=self.db) as db:
            self.assertEqual(db.conn.execute("SELECT status FROM review_executions").fetchone()[0], "needs_review")

    def test_other_partial_failures_are_not_replayed(self):
        with Database(db_path=self.db) as db:
            db.conn.execute("UPDATE review_executions SET error=error || '; 账户快照失败'")
            db.conn.commit()
        self.assertEqual(self.recovery(lambda _: self.fail("不得调用")), [])

    def test_response_format_and_truncation(self):
        with patch("strategy.mimo_client.post_chat_completion") as request:
            request.return_value = {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
            self.assertIsNone(_call_directive_llm("生成JSON"))
            self.assertEqual(request.call_args.kwargs["payload"]["response_format"], {"type": "json_object"})
            request.return_value = {"choices": [{"finish_reason": "stop", "message": {"reasoning_content": "{}"}}]}
            self.assertIsNone(_call_directive_llm("生成JSON"))

    def test_signal_reference_uses_timestamped_realtime_price(self):
        scores = [{"code": "002322", "latest_price": 13.08}]
        now = datetime(2026, 10, 14, 14, 15)
        quote = {"code": "002322", "price": 12.58, "timestamp": "2026-10-14 14:14:59"}
        _refresh_signal_prices(scores, lambda _: [quote], now=now)
        self.assertEqual(scores[0]["latest_price"], 12.58)
        self.assertEqual(scores[0]["daily_close"], 13.08)
        self.assertEqual(scores[0]["price_source"], "signal_realtime")
        _refresh_signal_prices(scores, lambda _: [quote], now=now + timedelta(minutes=10))
        self.assertFalse(scores[0]["signal_price_available"])

    def test_benchmark_uses_explicit_index_and_rejects_missing_endpoint(self):
        import pandas as pd
        from data.benchmark import load_hs300_history
        from review.daily_review import _fetch_hs300_daily_pct
        frame = pd.DataFrame({"data_date": ["2026-10-13", "2026-10-14"], "close": [4275.68, 4217.68]})
        with patch("data.kt_realtime.get_kt_client") as factory:
            factory.return_value.get_kline.return_value = frame
            clean = load_hs300_history("2026-10-14")
            factory.return_value.get_kline.assert_called_once_with("sh000300", period="d", count=1000)
        with patch("review.daily_review._load_hs300_history", return_value=clean), \
                patch("scheduler.market_calendar.is_trading_day", return_value=True):
            self.assertAlmostEqual(_fetch_hs300_daily_pct("2026-10-14"), 4217.68 / 4275.68 - 1)
            self.assertIsNone(_fetch_hs300_daily_pct("2026-10-15"))

    def test_benchmark_gap_recovers_from_account_snapshot_origin(self):
        import pandas as pd
        from review.daily_review import _compute_benchmark_pnl_pct
        root = Path(self.temp.name)/"reviews"
        root.mkdir()
        (root/"review_2026-10-12.json").write_text(json.dumps({"benchmark_pnl_pct": 0}), encoding="utf-8")
        (root/"review_2026-10-13.json").write_text(json.dumps({"benchmark_pnl_pct": None}), encoding="utf-8")
        frame = pd.DataFrame({"date": ["2026-10-12", "2026-10-14"], "close": [4287.98, 4217.68]})
        with patch("review.daily_review._load_hs300_history", return_value=frame):
            self.assertAlmostEqual(_compute_benchmark_pnl_pct("2026-10-14", str(root)), 4217.68 / 4287.98 - 1)

    def test_no_response_and_zero_confidence_are_daily_facts(self):
        with Database(db_path=self.db) as db:
            dims = {"capital": {"score": 50, "confidence": 0}}
            for scan in ("scan-1", "scan-2"):
                db.insert_candidate_outcome({"observation_key": scan, "scan_id": scan,
                    "observation_date": "2026-10-14", "observed_at": "2026-10-14T10:00:00",
                    "code": "002322", "entry_price": 13.08, "hold_reason": "HOLD_LLM(LLM无响应)",
                    "dimensions": json.dumps(dims)})
            db.insert_llm_decision({"date": "2026-10-14", "scan_id": "scan-1", "code": "002322",
                                    "action": "HOLD", "confidence": 0, "reasoning": "LLM无响应"})
            db.insert_llm_decision({"date": "2026-10-14", "scan_id": "scan-3", "code": "002322",
                                    "action": "HOLD", "confidence": .5, "reasoning": "主动观望", "dimensions": dims})
        with patch("scheduler.trader_brief._vibe_factors_summary", return_value={}), \
                patch("scheduler.trader_brief._text_sentiment_summary", return_value={}):
            facts = build_daily_facts("2026-10-14", self.db, now=self.now, market_status="盘后", trading_day=True)
        self.assertEqual(facts["llm_no_response_count"], 2)
        self.assertEqual(facts["funnel"]["llm_evaluated"], 1)
        self.assertFalse(facts["reviewed"])
        capital = next(d for d in facts["degradations"] if d["key"] == "capital")
        self.assertEqual(capital["degraded_observations"], 3)


if __name__ == "__main__":
    unittest.main()
