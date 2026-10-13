"""Tenant performance integration, immutable strategy versions and usage contracts."""
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient
from services import identity, workspace_accounts as accounts, workspace_observer as observer, workspace_usage, workspace_db, workspace_runner
from services.market_types import MarketError
from test_workspace_accounts import FakeQuotes, NOW


class WorkspaceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "ALPHAPILOT_WORKSPACE_DB": str(Path(self.temp.name) / "workspace.db"),
            "ALPHAPILOT_WORKSPACE_ENABLED": "1", "ALPHAPILOT_ENV": "production",
            "ALPHAPILOT_WORKSPACE_ORIGIN": "https://testserver", "ALPHAPILOT_ALLOW_REGISTRATION": "true",
            "ALPHAPILOT_MAIL_ENABLED": "false", "ALPHAPILOT_REQUIRE_VERIFIED_EMAIL": "false", "BROKER_MODE": "paper",
        })
        self.env.start()
        self.user = identity.register("owner@example.test", "Lifecycle-unique-2026!", "127.0.0.1").user
        self.other = identity.register("other@example.test", "Other-lifecycle-2026!", "127.0.0.2").user
        self.quotes = FakeQuotes()
        with patch.object(accounts, "utc_now", return_value=NOW):
            self.account = accounts.create_account(self.user["id"], "CRYPTO")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def configure(self, enabled=True, allocation="0.20", instrument="CRYPTO:BTC-USD"):
        return accounts.configure_strategy(self.user["id"], self.account["id"], enabled, instrument, allocation, self.quotes)

    def test_identical_save_preserves_execution_checkpoint_and_version(self):
        original = self.configure()
        workspace_runner.run_once(self.quotes, NOW)
        before = accounts.get_account(self.user["id"], self.account["id"])["strategy"]
        repeated = self.configure()
        self.assertEqual(original["version"], repeated["version"])
        self.assertEqual(before["last_bar_at"], repeated["last_bar_at"])
        workspace_runner.run_once(self.quotes, NOW + timedelta(minutes=3))
        self.assertEqual(len(accounts.get_account(self.user["id"], self.account["id"])["orders"]), 1)

    def test_strategy_edits_are_immutable_and_tenant_scoped(self):
        self.configure()
        self.configure(allocation="0.10")
        self.configure(enabled=False, allocation="0.10")
        history = accounts.strategy_history(self.user["id"], self.account["id"])
        self.assertEqual([v["version"] for v in history["versions"]], [3, 2, 1])
        self.assertEqual(history["versions"][-1]["max_allocation"], "0.20")
        self.assertTrue(history["versions"][-1]["enabled"])
        self.assertFalse(history["versions"][0]["enabled"])
        with self.assertRaises(MarketError):
            accounts.strategy_history(self.other["id"], self.account["id"])

    def test_instrument_change_cannot_abandon_old_strategy_position(self):
        self.configure()
        workspace_runner.run_once(self.quotes, NOW)
        with self.assertRaisesRegex(MarketError, "原策略标的"):
            self.configure(instrument="CRYPTO:ETH-USD")
        with self.assertRaisesRegex(MarketError, "原策略标的"):
            self.configure(enabled=False, instrument="CRYPTO:ETH-USD")
        self.assertFalse(self.configure(enabled=False)["enabled"])

    def test_verified_requirement_protects_domain_and_background_orders(self):
        self.configure()
        with patch.dict(os.environ, {"ALPHAPILOT_REQUIRE_VERIFIED_EMAIL": "true"}):
            with self.assertRaises(identity.IdentityError):
                accounts.create_account(self.user["id"], "US")
            with self.assertRaises(identity.IdentityError):
                accounts.place_order(self.user["id"], self.account["id"], "CRYPTO:BTC-USD", "buy", "1", "needs-verification", self.quotes, now=NOW)
            self.assertEqual(workspace_runner.run_once(self.quotes, NOW), [])
            self.assertFalse(self.configure(enabled=False)["enabled"])
            with workspace_db.connect() as conn, conn:
                conn.execute("UPDATE workspace_users SET email_verified=1 WHERE id=?", (self.user["id"],))
            conn.close()
            self.assertTrue(self.configure()["enabled"])

    def test_enabling_strategy_rechecks_valuation_age_inside_transaction(self):
        accounts.place_order(self.user["id"], self.account["id"], "CRYPTO:BTC-USD", "buy", "1", "age-check-order", self.quotes, now=NOW)
        quote = self.quotes.get_quote("CRYPTO", "BTC-USD", now=NOW)
        with patch.object(self.quotes, "get_quote", return_value=quote), patch.object(accounts, "utc_now", return_value=NOW + timedelta(seconds=31)):
            with self.assertRaisesRegex(MarketError, "已过期"):
                self.configure()

    def test_observer_captures_idle_accounts_and_throttles(self):
        first = observer.run_once(self.quotes, NOW + timedelta(minutes=5))
        self.assertEqual(first["captured"], 1)
        self.assertEqual(observer.run_once(self.quotes, NOW + timedelta(minutes=6))["captured"], 0)
        self.assertTrue(observer.status(NOW + timedelta(minutes=6))["running"])
        self.assertFalse(observer.status(NOW + timedelta(minutes=10))["running"])
        self.assertFalse(observer.status(NOW)["running"])

    def test_usage_counts_only_owned_committed_orders(self):
        accounts.place_order(self.user["id"], self.account["id"], "CRYPTO:BTC-USD", "buy", "1", "metered-order-001", self.quotes, now=NOW)
        accounts.place_order(self.user["id"], self.account["id"], "CRYPTO:BTC-USD", "buy", "1", "metered-order-001", self.quotes, now=NOW)
        value = workspace_usage.get_usage(self.user["id"], now=NOW)
        self.assertEqual(value["usage"]["filled_orders_this_month"], 1)
        self.assertFalse(value["billing"]["checkout_available"])
        self.assertEqual(workspace_usage.get_usage(self.other["id"], now=NOW)["usage"]["filled_orders_this_month"], 0)

    def test_performance_http_exports_and_private_cache_contract(self):
        import web.server
        server = importlib.reload(web.server)
        with TestClient(server.app, base_url="https://testserver") as client:
            auth = client.post("/api/workspace/auth/login", json={"email": "owner@example.test", "password": "Lifecycle-unique-2026!"})
            self.assertEqual(auth.status_code, 200)
            for endpoint in ("performance", "orders.csv", "snapshots.csv", "strategy/versions"):
                response = client.get(f"/api/workspace/accounts/{self.account['id']}/{endpoint}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                if endpoint.endswith(".csv"):
                    self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
                    self.assertIn("attachment", response.headers["content-disposition"])
            self.assertEqual(client.get("/api/workspace/billing").json()["usage"]["accounts"], 1)
            page = client.get("/workspace/")
            self.assertEqual(page.headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(page.headers["Cache-Control"], "no-store")
        with TestClient(server.app, base_url="https://testserver") as foreign:
            foreign.post("/api/workspace/auth/login", json={"email": "other@example.test", "password": "Other-lifecycle-2026!"})
            for endpoint in ("performance", "orders.csv", "snapshots.csv", "strategy/versions"):
                self.assertEqual(foreign.get(f"/api/workspace/accounts/{self.account['id']}/{endpoint}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
