"""邮件生命周期、密码恢复、会话撤销与旧数据库迁移；全部邮件使用 mock。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import smtplib
import ssl
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from services import identity, workspace_db, workspace_mail
from web.routers import identity as router


PREFIX = "/api/workspace"
PASSWORD = "safe orbit maple 728!"
NEW_PASSWORD = "new valley crane 941!"
MAIL_ENV = {
    "ALPHAPILOT_MAIL_ENABLED": "true", "ALPHAPILOT_WORKSPACE_ORIGIN": "https://app.example.test",
    "SMTP_HOST": "smtp.example.test", "SMTP_PORT": "587", "SMTP_USER": "test-mail-user",
    "SMTP_PASSWORD": "test-mail-password", "SMTP_FROM": "noreply@example.test", "SMTP_TLS_MODE": "starttls",
}


def app():
    application = FastAPI()
    application.include_router(router.router, prefix=PREFIX)

    @application.post(PREFIX + "/test-trading")
    def trading(user: dict = Depends(router.require_verified_trading_user)):
        return {"user_id": user["id"]}

    return application


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "workspace.sqlite3")
        self.env = patch.dict(os.environ, {
            **MAIL_ENV, "ALPHAPILOT_WORKSPACE_DB": self.database,
            "ALPHAPILOT_ENV": "development", "ENV": "development", "PRODUCTION": "false",
            "ALPHAPILOT_ALLOW_REGISTRATION": "true", "ALPHAPILOT_REQUIRE_VERIFIED_EMAIL": "true",
        })
        self.env.start()
        self.mail_patch = patch.object(workspace_mail, "send_action_email")
        self.send = self.mail_patch.start()
        self.client = TestClient(app(), base_url="https://app.example.test")
        response = self.client.post(PREFIX + "/auth/register", json={"email": "pilot@example.test", "password": PASSWORD})
        self.assertEqual(response.status_code, 201)
        self.uid = response.json()["user"]["id"]
        self.csrf = {"X-CSRF-Token": response.json()["csrf_token"]}
        self.token = self.client.cookies.get(router.SESSION_COOKIE)

    def tearDown(self):
        self.client.close()
        self.mail_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def verification(self):
        response = self.client.post(PREFIX + "/auth/email-verification/request", json={}, headers=self.csrf)
        self.assertEqual(response.status_code, 202, response.text)
        return self.send.call_args.args[2]

    def reset(self, email="pilot@example.test"):
        response = self.client.post(PREFIX + "/auth/password-reset/request", json={"email": email})
        self.assertEqual(response.status_code, 202, response.text)
        return self.send.call_args.args[2]

    def confirm_reset(self, token, password=NEW_PASSWORD):
        return self.client.post(PREFIX + "/auth/password-reset/confirm", json={"token": token, "new_password": password})

    def login(self, client=None, password=PASSWORD):
        return (client or self.client).post(PREFIX + "/auth/login", json={"email": "pilot@example.test", "password": password})

    def test_capabilities_reflect_configuration_and_production_policy_without_sending(self):
        result = self.client.get(PREFIX + "/auth/capabilities")
        self.assertEqual(result.json(), {"email_delivery_available": True, "registration_allowed": True, "require_verified_email": True})
        self.send.assert_not_called()
        with patch.dict(os.environ, {"ALPHAPILOT_MAIL_ENABLED": "false"}):
            self.assertFalse(self.client.get(PREFIX + "/auth/capabilities").json()["email_delivery_available"])
        with patch.dict(os.environ):
            os.environ.pop("ALPHAPILOT_REQUIRE_VERIFIED_EMAIL")
            self.assertFalse(identity.require_verified_email())
            os.environ["ALPHAPILOT_ENV"] = "production"
            self.assertTrue(identity.require_verified_email())

    def test_verification_unblocks_trading_only_after_explicit_confirmation(self):
        self.assertEqual(self.client.get(PREFIX + "/auth/email-verification/status").json()["status"], "not_requested")
        self.assertEqual(self.client.post(PREFIX + "/test-trading", headers=self.csrf).status_code, 403)
        token = self.verification()
        self.assertEqual(self.client.get(PREFIX + "/auth/email-verification/status").json()["status"], "sent")
        self.assertFalse(self.client.get(PREFIX + "/auth/me").json()["user"]["email_verified"])
        with closing(workspace_db.connect()) as connection:
            row = connection.execute("SELECT * FROM workspace_action_tokens").fetchone()
            self.assertEqual(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
            self.assertNotIn(token, dict(row).values())
        response = self.client.post(PREFIX + "/auth/email-verification/confirm", json={"token": token})
        self.assertEqual(response.json(), {"email_verified": True})
        self.assertEqual(self.client.get(PREFIX + "/auth/email-verification/status").json()["status"], "verified")
        self.assertEqual(self.client.post(PREFIX + "/test-trading", headers=self.csrf).status_code, 200)
        self.assertEqual(self.client.post(PREFIX + "/auth/email-verification/confirm", json={"token": token}).status_code, 422)

    def test_verification_tokens_expire_and_new_request_invalidates_previous(self):
        old = self.verification()
        latest = self.verification()
        self.assertNotEqual(old, latest)
        self.assertEqual(self.client.post(PREFIX + "/auth/email-verification/confirm", json={"token": old}).status_code, 422)
        with patch.object(identity.time, "time", return_value=time.time() + identity.EMAIL_VERIFICATION_TTL_SECONDS + 1):
            self.assertEqual(self.client.get(PREFIX + "/auth/email-verification/status").json()["status"], "expired")
            self.assertEqual(self.client.post(PREFIX + "/auth/email-verification/confirm", json={"token": latest}).status_code, 422)

    def test_verification_failure_is_visible_sanitized_and_cannot_be_consumed(self):
        self.send.side_effect = workspace_mail.MailError("private SMTP secret token=unsafe")
        response = self.client.post(PREFIX + "/auth/email-verification/request", headers=self.csrf)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("unsafe", response.text)
        self.assertNotIn("SMTP", response.text)
        token = self.send.call_args.args[2]
        self.assertEqual(self.client.get(PREFIX + "/auth/email-verification/status").json()["status"], "failed")
        self.assertEqual(self.client.post(PREFIX + "/auth/email-verification/confirm", json={"token": token}).status_code, 422)
        with closing(workspace_db.connect()) as connection:
            row = connection.execute("SELECT delivery_error,delivery_status FROM workspace_action_tokens").fetchone()
        self.assertEqual(tuple(row), ("delivery_failed", "failed"))

    def test_reset_response_does_not_enumerate_unknown_or_disabled_accounts(self):
        enabled = self.client.post(PREFIX + "/auth/password-reset/request", json={"email": "pilot@example.test"})
        self.assertEqual(self.send.call_count, 1)
        missing = self.client.post(PREFIX + "/auth/password-reset/request", json={"email": "missing@example.test"})
        self.assertEqual(self.send.call_count, 1)
        with closing(workspace_db.connect()) as connection, connection:
            connection.execute("UPDATE workspace_users SET is_active=0 WHERE id=?", (self.uid,))
        disabled = self.client.post(PREFIX + "/auth/password-reset/request", json={"email": "pilot@example.test"})
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual((enabled.status_code, enabled.json()), (202, missing.json()))
        self.assertEqual((disabled.status_code, disabled.json()), (202, enabled.json()))
        for response in (enabled, missing, disabled):
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn("token", response.text)

    def test_smtp_unconfigured_and_runtime_failure_keep_reset_response_generic(self):
        with patch.dict(os.environ, {"ALPHAPILOT_MAIL_ENABLED": "false"}):
            known = self.client.post(PREFIX + "/auth/password-reset/request", json={"email": "pilot@example.test"})
            unknown = self.client.post(PREFIX + "/auth/password-reset/request", json={"email": "missing@example.test"})
        self.assertEqual(known.status_code, 503)
        self.assertEqual(known.json(), unknown.json())
        self.send.side_effect = workspace_mail.MailError("private smtp response")
        response = self.client.post(PREFIX + "/auth/password-reset/request", json={"email": "pilot@example.test"})
        self.assertEqual(response.status_code, 202)
        self.assertNotIn("private", response.text)
        token = self.send.call_args.args[2]
        self.assertEqual(self.confirm_reset(token).status_code, 422)

    def test_reset_atomically_revokes_all_sessions_and_other_action_tokens(self):
        verification = self.verification()
        with TestClient(app(), base_url="https://app.example.test") as second:
            self.assertEqual(self.login(second).status_code, 200)
            token = self.reset()
            response = self.confirm_reset(token)
            self.assertEqual(response.status_code, 204, response.text)
            self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 401)
            self.assertEqual(second.get(PREFIX + "/auth/me").status_code, 401)
        self.assertEqual(self.confirm_reset(token).status_code, 422)
        self.assertEqual(self.client.post(PREFIX + "/auth/email-verification/confirm", json={"token": verification}).status_code, 422)
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login(password=NEW_PASSWORD).status_code, 200)
        with closing(workspace_db.connect()) as connection:
            self.assertEqual(connection.execute("SELECT credential_version FROM workspace_users WHERE id=?", (self.uid,)).fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workspace_action_tokens WHERE consumed_at IS NULL").fetchone()[0], 0)

    def test_reset_rejects_weak_password_without_consuming_valid_token(self):
        token = self.reset()
        self.assertEqual(self.confirm_reset(token, "weak").status_code, 422)
        self.assertEqual(self.confirm_reset(token).status_code, 204)

    def test_reset_expired_token_and_wrong_purpose_are_rejected(self):
        verification = self.verification()
        self.assertEqual(self.confirm_reset(verification).status_code, 422)
        reset = self.reset()
        self.assertEqual(self.client.post(PREFIX + "/auth/email-verification/confirm", json={"token": reset}).status_code, 422)
        with patch.object(identity.time, "time", return_value=time.time() + identity.PASSWORD_RESET_TTL_SECONDS + 1):
            self.assertEqual(self.confirm_reset(reset).status_code, 422)

    def test_concurrent_reset_consumption_succeeds_exactly_once(self):
        token = self.reset()
        def consume(index):
            try:
                identity.confirm_password_reset(token, NEW_PASSWORD, f"127.0.0.{index + 2}")
                return 204
            except identity.IdentityError as error:
                return error.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(consume, range(2)))
        self.assertEqual(sorted(statuses), [204, 422])

    def test_change_password_requires_current_password_and_revokes_all_sessions(self):
        reset = self.reset()
        wrong = self.client.post(PREFIX + "/auth/password/change", json={"current_password": "wrong-password", "new_password": NEW_PASSWORD}, headers=self.csrf)
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 200)
        self.assertIsNotNone(identity.get_session(self.token))
        with TestClient(app(), base_url="https://app.example.test") as second:
            self.assertEqual(self.login(second).status_code, 200)
            response = self.client.post(PREFIX + "/auth/password/change", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}, headers=self.csrf)
            self.assertEqual(response.status_code, 204, response.text)
            self.assertEqual(second.get(PREFIX + "/auth/me").status_code, 401)
        self.assertIsNone(identity.get_session(self.token))
        self.assertEqual(self.confirm_reset(reset).status_code, 422)
        self.assertEqual(self.login(password=NEW_PASSWORD).status_code, 200)

    def test_password_change_rechecks_session_after_slow_hash(self):
        original = identity.hash_password
        def revoke_during_hash(password):
            encoded = original(password)
            identity.revoke_session(self.token)
            return encoded
        with patch.object(identity, "hash_password", side_effect=revoke_during_hash):
            response = self.client.post(PREFIX + "/auth/password/change", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}, headers=self.csrf)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.login().status_code, 200)

    def test_session_list_and_revocation_are_owned_and_never_expose_hashes(self):
        with TestClient(app(), base_url="https://app.example.test") as second, TestClient(app(), base_url="https://app.example.test") as foreign:
            self.assertEqual(self.login(second).status_code, 200)
            stranger = foreign.post(PREFIX + "/auth/register", json={"email": "other@example.test", "password": PASSWORD}).json()
            sessions = self.client.get(PREFIX + "/auth/sessions").json()["sessions"]
            self.assertEqual(len(sessions), 2)
            self.assertEqual(sum(item["current"] for item in sessions), 1)
            for item in sessions:
                self.assertEqual(set(item), {"id", "created_at", "expires_at", "current"})
                self.assertNotEqual(item["id"], self.token)
                self.assertNotEqual(item["id"], hashlib.sha256(self.token.encode()).hexdigest())
            other_id = next(item["id"] for item in sessions if not item["current"])
            denied = foreign.delete(PREFIX + "/auth/sessions/" + other_id, headers={"X-CSRF-Token": stranger["csrf_token"]})
            self.assertEqual(denied.status_code, 404)
            self.assertEqual(self.client.delete(PREFIX + "/auth/sessions/" + other_id, headers=self.csrf).status_code, 204)
            self.assertEqual(second.get(PREFIX + "/auth/me").status_code, 401)
            current_id = next(item["id"] for item in sessions if item["current"])
            self.assertEqual(self.client.delete(PREFIX + "/auth/sessions/" + current_id, headers=self.csrf).status_code, 204)
            self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 401)

    def test_revoke_others_preserves_current_session(self):
        with TestClient(app(), base_url="https://app.example.test") as second:
            self.assertEqual(self.login(second).status_code, 200)
            self.assertEqual(self.client.post(PREFIX + "/auth/sessions/revoke-others", json={}, headers=self.csrf).status_code, 204)
            self.assertEqual(second.get(PREFIX + "/auth/me").status_code, 401)
            self.assertEqual(self.client.get(PREFIX + "/auth/me").status_code, 200)

    def test_disabled_user_cannot_consume_previously_delivered_links(self):
        verification = self.verification()
        reset = self.reset()
        with closing(workspace_db.connect()) as connection, connection:
            connection.execute("UPDATE workspace_users SET is_active=0 WHERE id=?", (self.uid,))
        self.assertEqual(self.confirm_reset(reset).status_code, 422)
        self.assertEqual(self.client.post(PREFIX + "/auth/email-verification/confirm", json={"token": verification}).status_code, 422)
        self.assertEqual(self.client.get(PREFIX + "/auth/sessions").status_code, 401)

    def test_reset_requests_are_limited_even_for_unknown_addresses_and_untrusted_ip_header(self):
        for _ in range(3):
            self.assertEqual(self.client.post(PREFIX + "/auth/password-reset/request", json={"email": "absent@example.test"}).status_code, 202)
        response = self.client.post(PREFIX + "/auth/password-reset/request", json={"email": "ABSENT@example.test"}, headers={"X-Forwarded-For": "203.0.113.4"})
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", response.headers)
        self.send.assert_not_called()

    def test_auth_mutations_check_csrf_origin_and_do_not_echo_secrets(self):
        self.assertEqual(self.client.post(PREFIX + "/auth/email-verification/request", json={}).status_code, 403)
        self.assertEqual(self.client.post(PREFIX + "/auth/password/change", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}).status_code, 403)
        for route, payload in (("password-reset/request", {"email": "pilot@example.test"}), ("password-reset/confirm", {"token": "secret", "new_password": NEW_PASSWORD}), ("email-verification/confirm", {"token": "secret"})):
            response = self.client.post(PREFIX + "/auth/" + route, json=payload, headers={"Origin": "https://evil.example"})
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("secret", response.text)
            self.assertNotIn(NEW_PASSWORD, response.text)

    def test_delayed_background_delivery_is_not_claimed_as_sent_or_retried_with_plaintext_storage(self):
        pending = identity.prepare_password_reset("pilot@example.test", "127.0.0.8")
        self.assertNotIn(pending.token, repr(pending))
        with patch.object(identity.time, "time", return_value=time.time() + identity.MAIL_PENDING_TIMEOUT_SECONDS + 1):
            identity.deliver_password_reset(pending)
        self.send.assert_not_called()
        with closing(workspace_db.connect()) as connection:
            row = connection.execute("SELECT delivery_status,delivery_error,consumed_at FROM workspace_action_tokens").fetchone()
        self.assertEqual(row["delivery_status"], "cancelled")
        self.assertIsNotNone(row["consumed_at"])

    def test_schema_upgrade_preserves_existing_sessions_and_is_concurrent(self):
        legacy = str(Path(self.temp.name) / "legacy.sqlite3")
        with closing(sqlite3.connect(legacy)) as connection, connection:
            connection.executescript("""
                CREATE TABLE workspace_users(id TEXT PRIMARY KEY,email TEXT UNIQUE,password_hash TEXT,plan TEXT DEFAULT 'free',email_verified INTEGER DEFAULT 0,is_active INTEGER DEFAULT 1,created_at INTEGER,updated_at INTEGER);
                CREATE TABLE workspace_sessions(token_hash TEXT PRIMARY KEY,user_id TEXT REFERENCES workspace_users(id),created_at INTEGER,expires_at INTEGER);
                INSERT INTO workspace_users(id,email,password_hash,created_at,updated_at) VALUES ('legacy','legacy@example.test','hash',1,1);
                INSERT INTO workspace_sessions VALUES ('legacy-token-hash','legacy',1,9999999999);
            """)
        with patch.dict(os.environ, {"ALPHAPILOT_WORKSPACE_DB": legacy}):
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda _: workspace_db.init_db(), range(8)))
            with closing(workspace_db.connect()) as connection:
                user = connection.execute("SELECT * FROM workspace_users").fetchone()
                session = connection.execute("SELECT * FROM workspace_sessions").fetchone()
                self.assertEqual(user["credential_version"], 1)
                self.assertEqual(session["token_hash"], "legacy-token-hash")
                self.assertTrue(session["session_id"])
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")


class MailTransportTests(unittest.TestCase):
    def test_smtp_starttls_is_verified_and_links_use_configured_origin_fragment(self):
        with patch.dict(os.environ, MAIL_ENV), patch.object(workspace_mail.smtplib, "SMTP") as constructor:
            smtp = constructor.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            token = "A" * 43
            workspace_mail.send_action_email("pilot@example.test", "reset_password", token)
            constructor.assert_called_once_with("smtp.example.test", 587, timeout=8)
            context = smtp.starttls.call_args.kwargs["context"]
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            calls = [call[0] for call in smtp.method_calls]
            self.assertLess(calls.index("starttls"), calls.index("login"))
            message = smtp.send_message.call_args.args[0]
            self.assertIn(f"https://app.example.test/workspace/#reset-password={token}", message.get_content())
            self.assertNotIn("?token=", message.get_content())

    def test_smtp_ssl_supported_and_insecure_or_missing_config_disabled(self):
        with patch.dict(os.environ, {**MAIL_ENV, "SMTP_TLS_MODE": "ssl", "SMTP_PORT": "465"}), patch.object(workspace_mail.smtplib, "SMTP_SSL") as constructor:
            smtp = constructor.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            workspace_mail.send_action_email("pilot@example.test", "verify_email", "B" * 43)
            smtp.starttls.assert_not_called()
            self.assertEqual(constructor.call_args.args, ("smtp.example.test", 465))
        for changes in ({"ALPHAPILOT_WORKSPACE_ORIGIN": ""}, {"ALPHAPILOT_WORKSPACE_ORIGIN": "http://app.example.test"}, {"ALPHAPILOT_WORKSPACE_ORIGIN": "https://app.example.test/path"}, {"SMTP_TLS_MODE": "plain"}, {"SMTP_PASSWORD": ""}, {"SMTP_FROM": "a@example.test\r\nBcc: x@example.test"}):
            with self.subTest(changes=tuple(changes)), patch.dict(os.environ, {**MAIL_ENV, **changes}):
                self.assertFalse(workspace_mail.available())

    def test_smtp_failure_is_sanitized_and_never_downgrades_tls(self):
        with patch.dict(os.environ, MAIL_ENV), patch.object(workspace_mail.smtplib, "SMTP") as constructor:
            smtp = constructor.return_value.__enter__.return_value
            smtp.starttls.side_effect = smtplib.SMTPNotSupportedError("private provider secret")
            with self.assertRaises(workspace_mail.MailError) as error:
                workspace_mail.send_action_email("pilot@example.test", "verify_email", "C" * 43)
            self.assertNotIn("private", str(error.exception))
            smtp.login.assert_not_called()
            smtp.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
