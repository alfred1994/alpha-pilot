"""注册、会话与同源写入的安全契约；只使用临时 SQLite 数据库。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from services import identity, workspace_db
from web.routers import identity as auth_router


PASSWORD = "orbit maple-river 736!"
PREFIX = "/api/workspace"


def _test_app():
    app = FastAPI()
    app.include_router(auth_router.router, prefix=PREFIX)

    @app.post(PREFIX + "/test-mutation")
    def mutate(user: dict = Depends(auth_router.require_mutation_user)):
        return {"owner_id": user["id"]}

    @app.get(PREFIX + "/test-repeated-dependency")
    def repeated_dependency(request: Request, user: dict = Depends(auth_router.require_user)):
        auth_router.require_user(request)
        return {"owner_id": user["id"]}

    return app


class WorkspaceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "workspace.sqlite3")
        self.environment = patch.dict(os.environ, {
            "ALPHAPILOT_WORKSPACE_DB": self.db_path,
            "ALPHAPILOT_WORKSPACE_ORIGIN": "",
            "ALPHAPILOT_ALLOW_REGISTRATION": "true",
            "ALPHAPILOT_ENV": "development",
            "ENV": "development",
            "PRODUCTION": "false",
        })
        self.environment.start()
        self.client = TestClient(_test_app(), base_url="https://testserver")

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.temp.cleanup()

    def register(self, email="pilot@example.com", **kwargs):
        return self.client.post(PREFIX + "/auth/register", json={"email": email, "password": PASSWORD}, **kwargs)

    def test_registration_normalizes_email_and_stores_only_hashed_secrets(self):
        response = self.register(" Pilot@Example.COM ")
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.assertEqual(payload["user"]["email"], "pilot@example.com")
        self.assertEqual(payload["user"]["plan"], "free")
        self.assertFalse(payload["user"]["email_verified"])
        self.assertEqual(payload["limits"]["markets"], ["CN", "US", "CRYPTO"])
        self.assertEqual(payload["limits"]["accounts"], 3)
        self.assertEqual(payload["limits"]["accounts_per_market"], 1)
        self.assertEqual(len(payload["csrf_token"]), 64)
        self.assertEqual(response.headers["cache-control"], "no-store")
        cookie = self.client.cookies.get(auth_router.SESSION_COOKIE)
        with closing(workspace_db.connect()) as connection:
            row = connection.execute("SELECT * FROM workspace_users").fetchone()
            self.assertNotIn(PASSWORD, row["password_hash"])
            self.assertTrue(row["password_hash"].startswith("scrypt$32768$8$1$"))
            self.assertTrue(identity.verify_password(PASSWORD, row["password_hash"]))
            session = connection.execute("SELECT * FROM workspace_sessions").fetchone()
            self.assertEqual(session["token_hash"], hashlib.sha256(cookie.encode()).hexdigest())
            self.assertNotIn(cookie, dict(session).values())
            self.assertNotIn(payload["csrf_token"], dict(session).values())
        self.assertEqual(self.client.get(PREFIX + "/auth/me").json(), payload)
        self.assertNotIn("password", response.text)

    def test_normalized_email_is_unique(self):
        self.assertEqual(self.register().status_code, 201)
        duplicate = self.register("PILOT@EXAMPLE.COM")
        self.assertEqual(duplicate.status_code, 409)
        with closing(workspace_db.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workspace_users").fetchone()[0], 1)

    def test_invalid_fields_and_client_plan_are_rejected_without_echoing_password(self):
        cases = [
            {"email": "invalid", "password": PASSWORD},
            {"email": "user@example.com", "password": "short"},
            {"email": "user@example.com", "password": "password123456"},
            {"email": "user@example.com", "password": "x" * 129},
            {"email": "user@example.com", "password": PASSWORD, "plan": "enterprise"},
            {"email": "user@example.com", "password": {"secret": PASSWORD}},
        ]
        for payload in cases:
            with self.subTest(payload_fields=tuple(payload)):
                response = self.client.post(PREFIX + "/auth/register", json=payload)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn(PASSWORD, response.text)
                self.assertNotIn("input", response.json())
        response = self.client.post(PREFIX + "/auth/register", content='{"email":"user@example.com","password":"invalid\\ud800-password"}', headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)

    def test_credential_bodies_are_bounded_and_json_only(self):
        form = self.client.post(PREFIX + "/auth/login", data={"email": "user@example.com", "password": PASSWORD})
        self.assertEqual(form.status_code, 422)
        oversized = self.client.post(PREFIX + "/auth/login", content=b" " * 4097, headers={"Content-Type": "application/json"})
        self.assertEqual(oversized.status_code, 422)
        malformed = self.client.post(PREFIX + "/auth/login", content=b"[invalid", headers={"Content-Type": "application/json"})
        self.assertEqual(malformed.status_code, 422)

    def test_registration_can_be_closed_without_disabling_existing_logins(self):
        self.assertEqual(self.register().status_code, 201)
        with patch.dict(os.environ, {"ALPHAPILOT_ALLOW_REGISTRATION": "false"}):
            self.assertEqual(self.register("new@example.com").status_code, 403)
            response = self.client.post(PREFIX + "/auth/login", json={"email": "pilot@example.com", "password": PASSWORD})
            self.assertEqual(response.status_code, 200)

    def test_login_uses_generic_failure_and_rotates_session(self):
        first = self.register().json()
        first_token = self.client.cookies.get(auth_router.SESSION_COOKIE)
        wrong = self.client.post(PREFIX + "/auth/login", json={"email": "pilot@example.com", "password": "wrong password"})
        absent = self.client.post(PREFIX + "/auth/login", json={"email": "absent@example.com", "password": "wrong password"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(absent.status_code, 401)
        self.assertEqual(wrong.json(), absent.json())
        success = self.client.post(PREFIX + "/auth/login", json={"email": "PILOT@example.com", "password": PASSWORD})
        self.assertEqual(success.status_code, 200)
        self.assertEqual(success.json()["user"], first["user"])
        self.assertNotEqual(success.json()["csrf_token"], first["csrf_token"])
        self.assertNotEqual(self.client.cookies.get(auth_router.SESSION_COOKIE), first_token)

    def test_sessions_use_secure_host_scoped_httponly_cookie(self):
        with patch.dict(os.environ, {"ALPHAPILOT_ENV": "production"}):
            response = self.register()
        self.assertEqual(response.status_code, 201)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Path=/api/workspace", cookie)
        self.assertNotIn("Domain=", cookie)

    def test_nonlocal_http_fails_closed_and_local_development_works(self):
        with TestClient(_test_app(), base_url="http://public.example.com") as client:
            response = client.post(PREFIX + "/auth/register", json={"email": "public@example.com", "password": PASSWORD})
            self.assertEqual(response.status_code, 403)
        with TestClient(_test_app(), base_url="http://localhost") as client:
            response = client.post(PREFIX + "/auth/register", json={"email": "local@example.com", "password": PASSWORD})
            self.assertEqual(response.status_code, 201)
            self.assertNotIn("Secure", response.headers["set-cookie"])
        with patch.dict(os.environ, {"ALPHAPILOT_ENV": "production"}):
            with TestClient(_test_app(), base_url="http://localhost") as client:
                response = client.post(PREFIX + "/auth/login", json={"email": "local@example.com", "password": PASSWORD})
                self.assertEqual(response.status_code, 403)

    def test_cross_origin_login_and_registration_are_denied(self):
        for action in ("register", "login"):
            for origin in ("https://evil.example", "null", "https://testserver.evil.example", "https://user@testserver"):
                with self.subTest(action=action, origin=origin):
                    response = self.client.post(PREFIX + "/auth/" + action, json={"email": "pilot@example.com", "password": PASSWORD}, headers={"Origin": origin})
                    self.assertEqual(response.status_code, 403)
        response = self.register(headers={"Origin": "https://testserver:443"})
        self.assertEqual(response.status_code, 201)

    def test_configured_external_origin_is_used_behind_trusted_proxy(self):
        with patch.dict(os.environ, {"ALPHAPILOT_WORKSPACE_ORIGIN": "https://app.example.com", "ALPHAPILOT_ENV": "production"}):
            with TestClient(_test_app(), base_url="http://internal:8000") as client:
                response = client.post(PREFIX + "/auth/register", json={"email": "proxy@example.com", "password": PASSWORD}, headers={"Origin": "https://app.example.com"})
                self.assertEqual(response.status_code, 201)
                self.assertIn("Secure", response.headers["set-cookie"])
                denied = client.post(PREFIX + "/auth/login", json={"email": "proxy@example.com", "password": PASSWORD}, headers={"Origin": "http://internal:8000"})
                self.assertEqual(denied.status_code, 403)

    def test_mutations_require_csrf_and_origin_and_logout_revokes_session(self):
        payload = self.register().json()
        token = self.client.cookies.get(auth_router.SESSION_COOKIE)
        self.assertEqual(self.client.post(PREFIX + "/test-mutation").status_code, 403)
        self.assertEqual(self.client.post(PREFIX + "/test-mutation", headers={"X-CSRF-Token": "wrong"}).status_code, 403)
        headers = {"X-CSRF-Token": payload["csrf_token"], "Origin": "https://testserver"}
        self.assertEqual(self.client.post(PREFIX + "/test-mutation", headers=headers).json()["owner_id"], payload["user"]["id"])
        denied = self.client.post(PREFIX + "/auth/logout", headers={**headers, "Origin": "https://evil.example"})
        self.assertEqual(denied.status_code, 403)
        denied = self.client.post(PREFIX + "/auth/logout", headers={**headers, "Sec-Fetch-Site": "same-site"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 200)
        logout = self.client.post(PREFIX + "/auth/logout", headers=headers)
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(logout.content, b"")
        self.assertIsNone(identity.get_session(token))
        self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 401)
        self.client.cookies.set(auth_router.SESSION_COOKIE, token)
        self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 401)

    def test_expired_and_disabled_sessions_are_denied(self):
        self.register()
        token = self.client.cookies.get(auth_router.SESSION_COOKIE)
        with closing(workspace_db.connect()) as connection, connection:
            connection.execute("UPDATE workspace_sessions SET expires_at = ?", (int(time.time()) - 1,))
        self.assertIsNone(identity.get_session(token))
        self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 401)
        login = self.client.post(PREFIX + "/auth/login", json={"email": "pilot@example.com", "password": PASSWORD})
        self.assertEqual(login.status_code, 200)
        with closing(workspace_db.connect()) as connection, connection:
            connection.execute("UPDATE workspace_users SET is_active = 0")
        self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 401)
        denied = self.client.post(PREFIX + "/auth/login", json={"email": "pilot@example.com", "password": PASSWORD})
        self.assertEqual(denied.status_code, 401)

    def test_user_and_session_csrf_are_isolated(self):
        first = self.register("alice@example.com").json()
        second = self.register("bob@example.com").json()
        self.assertNotEqual(first["user"]["id"], second["user"]["id"])
        denied = self.client.post(PREFIX + "/test-mutation", headers={"X-CSRF-Token": first["csrf_token"]})
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.post(PREFIX + "/test-mutation", headers={"X-CSRF-Token": second["csrf_token"]})
        self.assertEqual(allowed.json()["owner_id"], second["user"]["id"])

    def test_login_rate_limit_persists_across_clients_and_expires(self):
        payload = {"email": "missing@example.com", "password": PASSWORD}
        for _ in range(12):
            response = self.client.post(PREFIX + "/auth/login", json=payload)
            self.assertEqual(response.status_code, 401)
        with TestClient(_test_app(), base_url="https://testserver") as other:
            response = other.post(PREFIX + "/auth/login", json=payload, headers={"X-Forwarded-For": "203.0.113.5"})
            self.assertEqual(response.status_code, 429)
            self.assertGreater(int(response.headers["retry-after"]), 0)
        with closing(workspace_db.connect()) as connection, connection:
            connection.execute("UPDATE workspace_auth_limits SET expires_at = ?", (int(time.time()) - 1,))
        self.assertEqual(self.client.post(PREFIX + "/auth/login", json=payload).status_code, 401)

    def test_registration_rate_limit_includes_failed_weak_password_attempts(self):
        for _ in range(5):
            response = self.client.post(PREFIX + "/auth/register", json={"email": "pilot@example.com", "password": "short"})
            self.assertEqual(response.status_code, 422)
        response = self.register()
        self.assertEqual(response.status_code, 429)

    def test_database_initialization_is_concurrent_and_preserves_other_tables(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda _: workspace_db.init_db(), range(12)))
        with closing(workspace_db.connect()) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('preserve')")
        workspace_db.init_db()
        with closing(workspace_db.connect()) as connection:
            self.assertEqual(connection.execute("SELECT value FROM unrelated").fetchone()[0], "preserve")
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO workspace_sessions (token_hash,user_id,created_at,expires_at) VALUES ('invalid', 'missing', 0, 1)")

    def test_workspace_read_quota_is_per_user_and_persists_across_sessions(self):
        self.register()
        for _ in range(identity.WORKSPACE_READ_LIMIT):
            self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 200)
        denied = self.client.get(PREFIX + "/auth/me")
        self.assertEqual(denied.status_code, 429)
        self.assertTrue(0 < int(denied.headers["retry-after"]) <= 60)
        with TestClient(_test_app(), base_url="https://testserver") as other:
            login = other.post(PREFIX + "/auth/login", json={"email": "pilot@example.com", "password": PASSWORD})
            self.assertEqual(login.status_code, 200)
            self.assertEqual(other.get(PREFIX + "/auth/me").status_code, 429)
        self.assertEqual(self.register("second@example.com").status_code, 201)
        self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 200)

    def test_workspace_write_quota_is_separate_and_logout_remains_available(self):
        payload = self.register().json()
        headers = {"X-CSRF-Token": payload["csrf_token"]}
        for _ in range(identity.WORKSPACE_WRITE_LIMIT):
            self.assertEqual(self.client.post(PREFIX + "/test-mutation", headers=headers).status_code, 200)
        denied = self.client.post(PREFIX + "/test-mutation", headers=headers)
        self.assertEqual(denied.status_code, 429)
        self.assertIn("retry-after", denied.headers)
        self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 200)
        self.assertEqual(self.client.post(PREFIX + "/auth/logout", headers=headers).status_code, 204)
        self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 401)

    def test_read_quota_expires_and_cannot_block_logout(self):
        payload = self.register().json()
        for _ in range(identity.WORKSPACE_READ_LIMIT):
            self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 200)
        self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 429)
        with patch.object(identity.time, "time", return_value=time.time() + 61):
            self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 200)
        with closing(workspace_db.connect()) as connection, connection:
            connection.execute("UPDATE workspace_auth_limits SET attempts = ? WHERE scope = 'workspace_read'", (identity.WORKSPACE_READ_LIMIT,))
        self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 429)
        self.assertEqual(self.client.post(PREFIX + "/auth/logout", headers={"X-CSRF-Token": payload["csrf_token"]}).status_code, 204)

    def test_repeated_dependency_counts_only_once_per_request(self):
        payload = self.register().json()
        self.assertEqual(self.client.get(PREFIX + "/test-repeated-dependency").status_code, 200)
        with closing(workspace_db.connect()) as connection:
            count = connection.execute("SELECT attempts FROM workspace_auth_limits WHERE scope = 'workspace_read' AND key_hash = ?", (hashlib.sha256(payload["user"]["id"].encode()).hexdigest(),)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_database_failures_are_sanitized(self):
        with patch.object(identity, "register", side_effect=sqlite3.OperationalError("private/path token=secret")):
            response = self.register()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        self.assertNotIn("secret", response.text)


if __name__ == "__main__":
    unittest.main()
