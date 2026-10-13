"""离线净值/回撤/基准/导出：不触碰外部行情或现有运行数据。"""
import codecs
import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import workspace_accounts as accounts
from services import workspace_db
from services import workspace_performance as performance
from services.market_types import MarketError, Quote, get_instrument, utc_datetime


NOW = datetime(2026, 10, 12, 15, tzinfo=timezone.utc)


class Quotes:
    def __init__(self):
        self.prices = {"BTC-USD": Decimal("100"), "ETH-USD": Decimal("100"), "SPY": Decimal("100")}
        self.fail = set()
        self.calls = []
        self.callback = None
        self.timestamp_offset = timedelta(0)

    def get_quote(self, market, symbol, now=None):
        self.calls.append(symbol)
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
        if symbol in self.fail:
            raise MarketError("stale_quote", "报价过期")
        price = self.prices[symbol]
        moment = now if now is not None else NOW
        return Quote(get_instrument(market, symbol), price, moment + self.timestamp_offset,
                     moment, "offline-test", bid=price, ask=price)


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "ALPHAPILOT_WORKSPACE_DB": str(Path(self.tmp.name) / "workspace.sqlite3"),
            "BROKER_MODE": "paper", "ALPHAPILOT_PERFORMANCE_INTERVAL_SECONDS": "300",
        })
        self.env.start()
        accounts.initialize()
        performance.initialize()
        with workspace_db.connect() as conn:
            for user_id in ("owner", "other"):
                conn.execute("INSERT INTO workspace_users(id,email,password_hash,created_at,updated_at,email_verified) VALUES(?,?,?,?,?,1)",
                             (user_id, f"{user_id}@example.test", "not-a-real-password", 1, 1))
        conn.close()
        self.quotes = Quotes()
        self.account = self.create_account()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def create_account(self, market="CRYPTO", user="owner", seed=True, name=None):
        with patch.object(accounts, "utc_now", return_value=NOW):
            account = accounts.create_account(user, market, name=name)
        # 可独立运行，也兼容主代理已经接入创建账户时的 seed_snapshot。
        with workspace_db.connect() as conn:
            if seed:
                conn.execute("BEGIN IMMEDIATE")
                performance.seed_snapshot(conn, dict(account, user_id=user), NOW)
            else:
                conn.execute("DELETE FROM workspace_performance_snapshots WHERE account_id=?", (account["id"],))
        conn.close()
        return account

    def result(self, account=None, **kwargs):
        return performance.get_performance("owner", (account or self.account)["id"], now=NOW + timedelta(hours=3), **kwargs)

    def capture(self, minutes=5, reason="scheduled", account=None, quotes=None):
        return performance.capture_account("owner", (account or self.account)["id"], quotes or self.quotes,
                                           now=NOW + timedelta(minutes=minutes), reason=reason)

    def order(self, *, side="buy", symbol="ETH-USD", quantity="1", minutes=1, request="perf-order-1", account=None):
        selected = account or self.account
        return accounts.place_order("owner", selected["id"], f"{selected['market']}:{symbol}", side, quantity,
                                    request, self.quotes, now=NOW + timedelta(minutes=minutes))["order"]

    def assert_error(self, code, fn, *args, **kwargs):
        with self.assertRaises(MarketError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_cross_user_and_inactive_user_cannot_read_capture_or_export(self):
        for fn in (performance.get_performance, performance.orders_csv, performance.snapshots_csv):
            self.assert_error("not_found", fn, "other", self.account["id"])
        self.assert_error("not_found", performance.capture_account, "other", self.account["id"], self.quotes, now=NOW)
        self.assertEqual(self.quotes.calls, [])
        with workspace_db.connect() as conn:
            conn.execute("UPDATE workspace_users SET is_active=0 WHERE id='owner'")
        conn.close()
        self.assert_error("not_found", performance.get_performance, "owner", self.account["id"])
        self.assertEqual(performance.list_due_accounts(NOW), [])

    def test_initial_seed_is_atomic_idempotent_and_cannot_backfill_traded_accounts(self):
        initial = self.result()
        self.assertEqual(initial["summary"]["samples"], 1)
        self.assertEqual(initial["summary"]["last_equity"], "100000")
        self.assertEqual(initial["summary"]["total_return_pct"], "0.000000")
        self.assertTrue(initial["coverage"]["initial_snapshot"])
        old = self.create_account("US", seed=False)
        self.order(symbol="SPY", account=old)
        with workspace_db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.assert_error("invalid_initial_snapshot", performance.seed_snapshot, conn, dict(old, user_id="owner"), NOW)
            conn.rollback()
        conn.close()
        with patch.object(accounts, "utc_now", return_value=NOW), patch.object(performance, "seed_snapshot", side_effect=RuntimeError("rollback-test")):
            with self.assertRaises(RuntimeError):
                accounts.create_account("owner", "CN")
            self.assertFalse(any(item["market"] == "CN" for item in accounts.list_accounts("owner")))

    def test_duplicate_capture_is_network_free_and_trade_slot_does_not_overwrite(self):
        first = self.capture()
        count = len(self.quotes.calls)
        self.assertEqual(self.capture()["status"], "duplicate")
        self.assertEqual(self.capture(minutes=6)["status"], "duplicate")
        self.assertEqual(len(self.quotes.calls), count)
        event = self.capture(minutes=6, reason="trade")
        self.assertEqual(event["status"], "captured")
        self.assertNotEqual(first["snapshot_id"], event["snapshot_id"])
        self.assertEqual(self.result()["summary"]["samples"], 3)

    def test_fees_profit_and_negative_drawdown_match_recorded_equity(self):
        order = self.order()
        self.quotes.prices["ETH-USD"] = Decimal("120")
        self.capture(5)
        self.quotes.prices["ETH-USD"] = Decimal("80")
        self.capture(10)
        result = self.result()
        points = result["points"]
        cost = Decimal(order["price"]) + Decimal(order["fee"])
        self.assertEqual(Decimal(points[1]["equity"]), Decimal("100000") - cost + 120)
        self.assertEqual(Decimal(points[2]["equity"]), Decimal("100000") - cost + 80)
        self.assertLess(Decimal(points[2]["drawdown_pct"]), 0)
        expected = (((Decimal(points[2]["equity"]) / Decimal(points[1]["equity"])) - 1) * 100).quantize(Decimal("0.000001"))
        self.assertEqual(Decimal(result["summary"]["max_drawdown_pct"]), expected)
        self.assertLess(Decimal(result["summary"]["total_return_pct"]), 0)
        self.assertNotIn("annualized_return", result["summary"])

    def test_us_pending_cash_and_maturity_do_not_change_total_equity(self):
        account = self.create_account("US")
        self.order(symbol="SPY", account=account)
        self.order(side="sell", symbol="SPY", account=account, minutes=2, request="perf-sell-us")
        self.capture(5, account=account)
        first = self.result(account)["points"][-1]
        self.assertGreater(Decimal(first["pending_cash"]), 0)
        performance.capture_account("owner", account["id"], self.quotes, now=NOW + timedelta(days=1))
        last = self.result(account)["points"][-1]
        self.assertEqual(Decimal(last["pending_cash"]), 0)
        self.assertEqual(Decimal(last["cash"]), Decimal(first["cash"]) + Decimal(first["pending_cash"]))
        self.assertEqual(Decimal(last["equity"]), Decimal(first["equity"]))

    def test_account_change_during_network_skips_without_mixed_snapshot(self):
        self.order()
        self.quotes.callback = lambda: self.order(minutes=5, request="concurrent-buy")
        result = self.capture(5)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["code"], "account_changed")
        state = self.result()
        self.assertEqual(state["summary"]["samples"], 1)
        self.assertEqual(state["coverage"]["skipped_attempts"], 1)
        self.assertEqual(state["coverage"]["valuation_gaps"][0]["code"], "account_changed")

    def test_missing_or_stale_position_mark_never_creates_zero_or_flat_snapshot(self):
        self.order()
        self.quotes.fail.add("ETH-USD")
        skipped = self.capture(5)
        self.assertEqual(skipped["status"], "skipped")
        before = len(self.quotes.calls)
        self.assertEqual(self.capture(6)["status"], "duplicate")
        self.assertEqual(len(self.quotes.calls), before)
        self.quotes.fail.clear()
        self.quotes.timestamp_offset = -timedelta(minutes=1)
        self.assertEqual(self.capture(10)["code"], "stale_quote")
        result = self.result()
        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(result["coverage"]["skipped_attempts"], 2)
        self.assertEqual(len(result["coverage"]["valuation_gaps"]), 2)

    def test_quote_expiring_while_waiting_for_write_lock_cannot_be_recorded(self):
        self.order()
        observed = NOW + timedelta(minutes=5)
        source_stamp = observed - timedelta(seconds=29)
        commit_time = observed + timedelta(seconds=2)
        times = iter((observed, observed, commit_time))
        source_quotes = []

        def clock(value=None):
            return next(times) if value is None else utc_datetime(value)

        def fetch(market, symbol, now=None):
            price = self.quotes.prices[symbol]
            quote = Quote(get_instrument(market, symbol), price, source_stamp, observed, "offline-lock-test")
            source_quotes.append(quote)
            return quote

        with patch.object(performance, "utc_datetime", side_effect=clock), patch.object(self.quotes, "get_quote", side_effect=fetch):
            outcome = performance.capture_account("owner", self.account["id"], self.quotes)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["code"], "stale_quote")
        self.assertEqual(outcome["as_of"], commit_time.isoformat(timespec="microseconds"))
        self.assertTrue(all(quote.as_of == source_stamp for quote in source_quotes))
        self.assertEqual(self.result()["summary"]["samples"], 1)

    def test_benchmark_expiring_at_write_lock_is_unavailable_without_invented_price(self):
        observed = NOW + timedelta(minutes=5)
        commit_time = observed + timedelta(seconds=31)
        times = iter((observed, observed, commit_time))
        source = Quote(get_instrument("CRYPTO", "BTC-USD"), Decimal("100"), observed, observed, "offline-lock-test")

        def clock(value=None):
            return next(times) if value is None else utc_datetime(value)

        with patch.object(performance, "utc_datetime", side_effect=clock), patch.object(self.quotes, "get_quote", return_value=source):
            outcome = performance.capture_account("owner", self.account["id"], self.quotes)
        self.assertEqual(outcome["status"], "captured")
        self.assertEqual(source.as_of, observed)
        result = self.result()
        self.assertEqual(result["points"][-1]["equity"], "100000")
        self.assertIsNone(result["points"][-1]["benchmark_return_pct"])
        self.assertIsNone(result["points"][-1]["benchmark_as_of"])
        with workspace_db.connect() as conn:
            row = conn.execute("SELECT benchmark_error FROM workspace_performance_snapshots WHERE id=?", (outcome["snapshot_id"],)).fetchone()
        conn.close()
        self.assertEqual(row["benchmark_error"], "stale_quote")

    def test_missing_benchmark_keeps_real_cash_snapshot_and_null_comparison(self):
        self.quotes.fail.add("BTC-USD")
        self.assertEqual(self.capture()["status"], "captured")
        result = self.result()
        self.assertEqual(result["points"][-1]["equity"], "100000")
        self.assertIsNone(result["points"][-1]["benchmark_return_pct"])
        self.assertIsNone(result["points"][-1]["comparison_return_pct"])
        self.assertEqual(result["benchmark"]["status"], "unavailable")
        self.assertEqual(result["coverage"]["missing_benchmark_samples"], 2)

    def test_benchmark_and_account_comparison_start_at_first_paired_capture(self):
        self.order()
        self.capture(5)
        self.quotes.prices["BTC-USD"] = Decimal("110")
        self.quotes.prices["ETH-USD"] = Decimal("120")
        self.capture(10)
        result = self.result()
        self.assertEqual(result["benchmark"]["baseline_as_of"], (NOW + timedelta(minutes=5)).isoformat(timespec="microseconds"))
        self.assertEqual(result["points"][1]["benchmark_return_pct"], "0.000000")
        self.assertEqual(result["points"][1]["comparison_return_pct"], "0.000000")
        self.assertLess(Decimal(result["points"][1]["return_pct"]), 0)
        self.assertEqual(result["points"][2]["benchmark_return_pct"], "10.000000")
        self.assertNotEqual(result["points"][2]["comparison_return_pct"], result["points"][2]["return_pct"])
        self.assertEqual(result["benchmark"]["method"], "price_return")
        self.assertEqual(result["benchmark"]["source"], "offline-test")

    def test_position_snapshots_preserve_quantity_and_source_timestamp(self):
        self.order(quantity="0.12345678")
        self.quotes.timestamp_offset = -timedelta(seconds=2)
        capture = self.capture()
        with workspace_db.connect() as conn:
            row = conn.execute("SELECT * FROM workspace_performance_snapshots WHERE id=?", (capture["snapshot_id"],)).fetchone()
        conn.close()
        position = json.loads(row["positions_json"])[0]
        self.assertEqual(position["quantity"], "0.12345678")
        self.assertEqual(position["quote_as_of"], (NOW + timedelta(minutes=5, seconds=-2)).isoformat(timespec="microseconds"))
        self.assertEqual(Decimal(position["market_value"]), Decimal("12.34567800"))

    def test_utc_normalization_and_naive_time_rejection(self):
        beijing = (NOW + timedelta(minutes=5)).astimezone(timezone(timedelta(hours=8)))
        performance.capture_account("owner", self.account["id"], self.quotes, now=beijing)
        self.assertEqual(self.result()["points"][-1]["as_of"], (NOW + timedelta(minutes=5)).isoformat(timespec="microseconds"))
        self.assert_error("invalid_timestamp", performance.capture_account, "owner", self.account["id"], self.quotes, now=NOW.replace(tzinfo=None))

    def test_page_is_latest_and_summary_uses_full_observed_history(self):
        for minute in (5, 10, 25):
            self.capture(minute)
        all_points = self.result()
        page = self.result(limit=2)
        self.assertEqual(page["points"], all_points["points"][-2:])
        self.assertEqual(page["summary"], all_points["summary"])
        self.assertEqual(page["pagination"]["next_offset"], 2)
        older = self.result(limit=2, offset=2)
        self.assertEqual(older["points"], all_points["points"][:2])
        self.assertFalse(older["pagination"]["has_more"])
        self.assertEqual(page["coverage"]["gap_count"], 1)
        self.assertEqual(page["coverage"]["gaps"][0]["missing_intervals"], 2)
        self.assertTrue(page["points"][-1]["gap_before"])
        self.assert_error("invalid_pagination", performance.get_performance, "owner", self.account["id"], limit=2001)

    def test_due_accounts_are_throttled_after_success_and_failure(self):
        self.assertIn(self.account["id"], {row["id"] for row in performance.list_due_accounts(NOW)})
        self.order()
        self.quotes.fail.add("ETH-USD")
        self.capture(5)
        self.assertEqual(performance.list_due_accounts(NOW + timedelta(minutes=6)), [])
        self.assertIn(self.account["id"], {row["id"] for row in performance.list_due_accounts(NOW + timedelta(minutes=10))})
        self.assert_error("invalid_limit", performance.list_due_accounts, NOW, 101)

    def test_concurrent_capture_claim_prevents_duplicate_network_and_writes(self):
        started, release = threading.Event(), threading.Event()
        self.quotes.callback = lambda: (started.set(), release.wait(5))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.capture)
            self.assertTrue(started.wait(5))
            second = pool.submit(self.capture)
            result = second.result(timeout=5)
            self.assertEqual(result["status"], "duplicate")
            release.set()
            self.assertEqual(first.result(timeout=5)["status"], "captured")
        self.assertEqual(self.quotes.calls, ["BTC-USD"])
        self.assertEqual(self.result()["summary"]["samples"], 2)

    def test_readers_do_not_write_or_call_network(self):
        self.capture()
        before_calls = len(self.quotes.calls)
        with patch.object(performance, "_ensure_schema", side_effect=AssertionError("read wrote schema")):
            self.result()
            performance.orders_csv("owner", self.account["id"])
            performance.snapshots_csv("owner", self.account["id"])
        self.assertEqual(len(self.quotes.calls), before_calls)
        with workspace_db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workspace_performance_snapshots").fetchone()[0], 2)
        conn.close()

    def test_csv_bom_exact_decimal_text_and_formula_injection(self):
        order = self.order(quantity="0.12345678")
        self.capture()
        with workspace_db.connect() as conn:
            conn.execute("UPDATE workspace_accounts SET name=? WHERE id=?", ('=HYPERLINK("bad","name")', self.account["id"]))
            conn.execute("UPDATE workspace_orders SET source=? WHERE id=?", (" \t@SUM(1,2)", order["id"]))
        conn.close()
        payload = performance.orders_csv("owner", self.account["id"])
        self.assertTrue(payload.startswith(codecs.BOM_UTF8))
        row = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))[0]
        self.assertEqual(row["quantity"], "0.12345678")
        self.assertTrue(row["account_name"].startswith("'="))
        self.assertTrue(row["source"].startswith("' \t@"))
        self.assertEqual(row["price"], order["price"])
        snapshots = performance.snapshots_csv("owner", self.account["id"])
        rows = list(csv.DictReader(io.StringIO(snapshots.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["benchmark_return_pct"], "")
        self.assertTrue(rows[1]["account_name"].startswith("'="))
        self.assertEqual(json.loads(rows[1]["positions_json"])[0]["quantity"], "0.12345678")

    def test_exports_fail_explicitly_instead_of_truncating(self):
        self.order()
        self.order(request="second-export-order", minutes=2)
        self.capture()
        self.assert_error("export_limit", performance.orders_csv, "owner", self.account["id"], max_rows=1)
        self.assert_error("export_limit", performance.snapshots_csv, "owner", self.account["id"], max_rows=1)
        self.assert_error("invalid_limit", performance.orders_csv, "owner", self.account["id"], max_rows=100001)

    def test_existing_account_does_not_invent_a_creation_snapshot(self):
        account = self.create_account("US", seed=False)
        self.assertEqual(self.result(account)["points"], [])
        self.assertIsNone(self.result(account)["summary"]["total_return_pct"])
        self.capture(5, account=account)
        result = self.result(account)
        self.assertEqual(result["summary"]["samples"], 1)
        self.assertFalse(result["coverage"]["initial_snapshot"])
        self.assertEqual(result["summary"]["first_at"], (NOW + timedelta(minutes=5)).isoformat(timespec="microseconds"))


if __name__ == "__main__":
    unittest.main()
