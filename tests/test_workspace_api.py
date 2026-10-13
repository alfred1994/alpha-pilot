"""HTTP boundaries for identity, tenant portfolios and public dashboard coexistence."""
import importlib
import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient
from test_workspace_accounts import FakeQuotes
from web.routers.workspace import get_quotes


class WorkspaceApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ALPHAPILOT_WORKSPACE_DB": str(Path(self.tmp.name) / "workspace.db"), "BROKER_MODE": "paper", "ALPHAPILOT_ENV": "production", "ALPHAPILOT_REQUIRE_VERIFIED_EMAIL": "0", "ALPHAPILOT_WORKSPACE_ENABLED": "1", "ALPHAPILOT_WORKSPACE_ORIGIN": "https://testserver", "ALPHAPILOT_ALLOW_REGISTRATION": "true"})
        self.env.start()
        import web.server
        self.server = importlib.reload(web.server)
        self.server.app.dependency_overrides[get_quotes] = lambda: FakeQuotes()
        self.client = TestClient(self.server.app, base_url="https://testserver")
        self.other = TestClient(self.server.app, base_url="https://testserver")
        response = self.client.post("/api/workspace/auth/register", json={"email": "owner@example.test", "password": "Workspace-secure-2026!"}, headers={"Origin": "https://testserver"})
        self.assertEqual(response.status_code, 201)
        self.headers = {"Origin": "https://testserver", "X-CSRF-Token": response.json()["csrf_token"]}

    def tearDown(self):
        self.client.close()
        self.other.close()
        self.server.app.dependency_overrides.clear()
        self.env.stop()
        self.tmp.cleanup()

    def create(self):
        response = self.client.post("/api/workspace/accounts", json={"market": "CRYPTO"}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_anonymous_users_cannot_view_accounts(self):
        self.assertEqual(self.other.get("/api/workspace/overview").status_code, 401)
        self.assertEqual(self.other.get("/api/workspace/markets").status_code, 401)
        response = self.client.get("/api/workspace/overview")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["accounts"], [])

    def test_authenticated_create_requires_csrf_and_no_privilege_fields(self):
        response = self.client.post("/api/workspace/accounts", json={"market": "US"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/workspace/accounts", json={"market": "US", "cash": "999999", "user_id": "other", "plan": "pro"}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/api/workspace/overview").json()["accounts"], [])

    def test_foreign_account_has_no_details(self):
        account_id = self.create()
        registered = self.other.post("/api/workspace/auth/register", json={"email": "other@example.test", "password": "Other-secure-2026!"}, headers={"Origin": "https://testserver"})
        self.assertEqual(registered.status_code, 201)
        self.assertEqual(self.other.get(f"/api/workspace/accounts/{account_id}").status_code, 404)
        headers = {"Origin": "https://testserver", "X-CSRF-Token": registered.json()["csrf_token"]}
        response = self.other.post(f"/api/workspace/accounts/{account_id}/orders", json={"instrument_id": "CRYPTO:BTC-USD", "side": "buy", "quantity": "1", "request_id": "foreign-order-attempt"}, headers=headers)
        self.assertEqual(response.status_code, 404)

    def test_public_dashboard_control_stays_unmounted(self):
        self.assertEqual(self.client.post("/api/control/pause", json={}).status_code, 404)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/workspace/").status_code, 200)
        self.assertIn("script-src 'self'", self.client.get("/workspace/").headers["content-security-policy"])

    def test_registration_and_logout_are_real_cookie_sessions(self):
        self.assertEqual(self.client.get("/api/workspace/auth/me").status_code, 200)
        self.assertEqual(self.client.post("/api/workspace/auth/logout", headers=self.headers).status_code, 204)
        self.assertEqual(self.client.get("/api/workspace/auth/me").status_code, 401)

    def test_account_storage_error_has_no_internal_details(self):
        with patch("services.workspace_accounts.list_accounts", side_effect=sqlite3.OperationalError("private/path secret=value")):
            response = self.client.get("/api/workspace/overview")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        self.assertNotIn("secret", response.text)

    def test_production_workspace_can_be_disabled_without_legacy_control(self):
        with patch.dict(os.environ, {"ALPHAPILOT_WORKSPACE_ENABLED": "0"}):
            server = importlib.reload(self.server)
            with TestClient(server.app, base_url="https://testserver") as client:
                self.assertEqual(client.get("/api/workspace/overview").status_code, 503)
                self.assertEqual(client.post("/api/control/pause", json={}).status_code, 404)
                self.assertEqual(client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
