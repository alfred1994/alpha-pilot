"""Offline customer-account isolation, atomic money and strategy contracts."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import identity, workspace_accounts as store, workspace_runner as runner
from services.market_types import Bar, MarketError, Quote, get_instrument


NOW = datetime(2026, 10, 12, 15, tzinfo=timezone.utc)


class FakeQuotes:
    def __init__(self, price="100"):
        self.price = Decimal(price)
        self.calls = 0
        self.failure = False
        self.bar_count = 60
        self.before_bars = None

    def get_quote(self, market, symbol, now=None):
        self.calls += 1
        if self.failure:
            raise MarketError("quote_unavailable", "行情不可用")
        stamp = now or datetime.now(timezone.utc)
        return Quote(get_instrument(market, symbol), self.price, stamp, stamp, "offline-test", bid=self.price, ask=self.price)

    def get_bars(self, market, symbol, limit=80, now=None):
        if self.before_bars:
            self.before_bars()
        now = now or NOW
        return [Bar((now - timedelta(days=self.bar_count - i)).replace(hour=0, minute=0, second=0, microsecond=0), Decimal(40 + i)) for i in range(self.bar_count)]


class WorkspaceAccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ALPHAPILOT_WORKSPACE_DB": str(Path(self.tmp.name) / "workspace.sqlite3"), "BROKER_MODE": "paper", "ALPHAPILOT_ALLOW_REGISTRATION": "true"})
        self.env.start()
        self.uid = identity.register("owner@example.test", "My-paper-account-2026!", "127.0.0.1").user["id"]
        self.other = identity.register("other@example.test", "Other-paper-account-2026!", "127.0.0.2").user["id"]
        self.quotes = FakeQuotes()
        self.a = store.create_account(self.uid, "CRYPTO")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def order(self, side="buy", quantity="1", request_id="test-request-1", account=None, iid="CRYPTO:BTC-USD", now=NOW):
        return store.place_order(self.uid, (account or self.a)["id"], iid, side, quantity, request_id, self.quotes, now=now)

    def test_accounts_are_owned_and_cannot_share_a_currency_balance(self):
        with self.assertRaisesRegex(MarketError, "不存在"):
            store.get_account(self.other, self.a["id"], quotes=self.quotes)
        with self.assertRaisesRegex(MarketError, "不存在"):
            store.place_order(self.other, self.a["id"], "CRYPTO:BTC-USD", "buy", "1", "foreign-request", self.quotes, now=NOW)
        self.assertEqual(self.quotes.calls, 0)
        self.assertEqual(store.list_accounts(self.other), [])
        with self.assertRaisesRegex(MarketError, "不一致"):
            self.order(iid="US:SPY")

    def test_one_account_per_market_and_server_allocated_funds(self):
        with self.assertRaisesRegex(MarketError, "已有"):
            store.create_account(self.uid, "CRYPTO")
        for market in ("US", "CN"):
            self.assertEqual(store.create_account(self.uid, market)["cash"], "100000")
        self.assertEqual(len(store.list_accounts(self.uid)), 3)

    def test_fractional_crypto_order_and_ledger_reconcile(self):
        filled = self.order(quantity="0.12345678")["order"]
        from decimal import ROUND_CEILING
        cost = (Decimal(filled["quantity"]) * Decimal(filled["price"])).quantize(Decimal("0.01"), rounding=ROUND_CEILING) + Decimal(filled["fee"])
        snapshot = store.get_account(self.uid, self.a["id"], quotes=self.quotes, now=NOW)
        self.assertEqual(Decimal(snapshot["account"]["cash"]), Decimal(100000) - cost)
        self.assertEqual(Decimal(snapshot["positions"][0]["quantity"]), Decimal("0.12345678"))
        with store.connection() as conn:
            ledger = conn.execute("SELECT cash_delta FROM workspace_ledger WHERE account_id=?", (self.a["id"],)).fetchall()
        self.assertEqual(sum(Decimal(r[0]) for r in ledger), Decimal(snapshot["account"]["cash"]))

    def test_retry_reuses_fill_even_if_provider_is_down(self):
        first = self.order()
        self.quotes.failure = True
        second = self.order()
        self.assertTrue(second["replayed"])
        self.assertEqual(first["order"]["id"], second["order"]["id"])
        with self.assertRaisesRegex(MarketError, "其他委托"):
            self.order(quantity="2")

    def test_concurrent_identical_orders_only_charge_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.order(), range(4)))
        self.assertEqual(len({r["order"]["id"] for r in results}), 1)
        self.assertEqual(len(store.get_account(self.uid, self.a["id"])["orders"]), 1)

    def test_concurrent_orders_cannot_overspend(self):
        def buy(i):
            try:
                return self.order(quantity="700", request_id=f"large-request-{i}")
            except MarketError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(buy, range(2)))
        self.assertEqual(outcomes.count("insufficient_funds"), 1)
        self.assertGreaterEqual(Decimal(store.get_account(self.uid, self.a["id"])["account"]["cash"]), 0)

    def test_market_errors_and_oversells_do_not_mutate_balances(self):
        self.quotes.failure = True
        with self.assertRaises(MarketError):
            self.order()
        self.quotes.failure = False
        with self.assertRaisesRegex(MarketError, "不足"):
            self.order(side="sell")
        snapshot = store.get_account(self.uid, self.a["id"])
        self.assertEqual(snapshot["account"]["cash"], "100000")
        self.assertEqual(snapshot["orders"], [])

    def test_unknown_mark_is_unavailable_not_zero_or_fake_equity(self):
        self.order()
        self.quotes.failure = True
        snapshot = store.get_account(self.uid, self.a["id"], quotes=self.quotes, now=NOW)
        self.assertIsNone(snapshot["account"]["equity"])
        self.assertIsNone(snapshot["positions"][0]["market_value"])

    def test_cn_t_plus_one_failure_rolls_back_lot_changes(self):
        account = store.create_account(self.uid, "CN")
        day1 = NOW.replace(hour=2)
        day2 = day1 + timedelta(days=1)
        self.order(quantity="100", account=account, iid="CN:510300", now=day1)
        self.order(quantity="100", account=account, iid="CN:510300", now=day2, request_id="second-buy-cn")
        with self.assertRaisesRegex(MarketError, "下一交易日"):
            self.order(side="sell", quantity="200", account=account, iid="CN:510300", now=day2, request_id="sell-all-cn")
        snapshot = store.get_account(self.uid, account["id"], now=day2)
        self.assertEqual(Decimal(snapshot["positions"][0]["quantity"]), 200)
        self.order(side="sell", quantity="100", account=account, iid="CN:510300", now=day2, request_id="sell-available-cn")

    def test_us_sell_proceeds_settle_next_trading_day(self):
        account = store.create_account(self.uid, "US")
        self.order(quantity="1", account=account, iid="US:SPY")
        sold = self.order(side="sell", quantity="1", account=account, iid="US:SPY", request_id="us-sell-request")
        snapshot = store.get_account(self.uid, account["id"], now=NOW)["account"]
        self.assertGreater(Decimal(snapshot["pending_cash"]), 0)
        next_day = store.get_account(self.uid, account["id"], now=NOW + timedelta(days=1))["account"]
        self.assertEqual(Decimal(next_day["pending_cash"]), 0)
        self.assertEqual(Decimal(next_day["cash"]), Decimal(snapshot["cash"]) + Decimal(sold["order"]["price"]) - Decimal(sold["order"]["fee"]))

    def enable(self):
        return store.configure_strategy(self.uid, self.a["id"], True, "CRYPTO:BTC-USD", "0.20", self.quotes)

    def test_strategy_runs_once_per_completed_bar_with_allocation_cap(self):
        self.enable()
        self.assertEqual(runner.run_once(self.quotes, NOW)[0]["status"], "executed")
        runner.run_once(self.quotes, NOW + timedelta(minutes=1))
        snapshot = store.get_account(self.uid, self.a["id"], quotes=self.quotes, now=NOW + timedelta(minutes=1))
        self.assertEqual(len(snapshot["orders"]), 1)
        self.assertLessEqual(Decimal(snapshot["positions"][0]["market_value"]), 20000)
        self.assertTrue(snapshot["worker"]["running"])

    def test_insufficient_bars_are_blocked(self):
        self.enable()
        self.quotes.bar_count = 20
        self.assertEqual(runner.run_once(self.quotes, NOW)[0]["status"], "blocked")
        self.assertFalse(store.get_account(self.uid, self.a["id"])["orders"])

    def test_crash_after_fill_recovers_completed_bar(self):
        self.enable()
        with patch.object(runner, "_finish"):
            self.assertEqual(runner.run_once(self.quotes, NOW)[0]["status"], "executed")
        self.assertEqual(runner.run_once(self.quotes, NOW + timedelta(minutes=3))[0]["status"], "executed")
        snapshot = store.get_account(self.uid, self.a["id"])
        self.assertEqual(len(snapshot["orders"]), 1)
        self.assertIsNotNone(snapshot["strategy"]["last_bar_at"])

    def test_pause_during_calculation_prevents_order(self):
        self.enable()
        self.quotes.before_bars = lambda: store.configure_strategy(self.uid, self.a["id"], False, "CRYPTO:BTC-USD", "0.20", self.quotes)
        runner.run_once(self.quotes, NOW)
        snapshot = store.get_account(self.uid, self.a["id"])
        self.assertFalse(snapshot["orders"])
        self.assertFalse(snapshot["strategy"]["enabled"])

    def test_disabled_user_cannot_run_background_strategy(self):
        self.enable()
        with store.connection() as conn, conn:
            conn.execute("UPDATE workspace_users SET is_active=0 WHERE id=?", (self.uid,))
        self.assertEqual(runner.run_once(self.quotes, NOW), [])
        with self.assertRaises(MarketError):
            self.order()

    def test_commit_revalidates_quote_after_wait(self):
        from services.market_types import get_market_spec
        expired = NOW + timedelta(seconds=get_market_spec("CRYPTO").max_quote_age_seconds + 1)
        # The provider test double returns NOW, while execution has advanced past its TTL.
        quote = self.quotes.get_quote("CRYPTO", "BTC-USD", now=NOW)
        with patch.object(store, "utc_now", return_value=expired), patch.object(self.quotes, "get_quote", return_value=quote):
            with self.assertRaises(MarketError):
                store.place_order(self.uid, self.a["id"], "CRYPTO:BTC-USD", "buy", "1", "expired-after-wait", self.quotes)
        self.assertEqual(store.get_account(self.uid, self.a["id"])["orders"], [])

    def test_drawdown_pause_survives_user_pause_and_cannot_be_reset(self):
        self.enable()
        runner.run_once(self.quotes, NOW)
        self.quotes.price = Decimal(40)
        self.assertEqual(runner.run_once(self.quotes, NOW + timedelta(minutes=1))[0]["status"], "risk_paused")
        store.configure_strategy(self.uid, self.a["id"], False, "CRYPTO:BTC-USD", "0.20", self.quotes)
        with self.assertRaisesRegex(MarketError, "回撤"):
            self.enable()

    def test_real_mode_is_never_accepted(self):
        with patch.dict(os.environ, {"BROKER_MODE": "real"}):
            with self.assertRaisesRegex(MarketError, "仅支持模拟盘"):
                self.order()


if __name__ == "__main__":
    unittest.main()
