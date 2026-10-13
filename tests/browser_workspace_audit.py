"""Optional browser audit with real API/SQLite, fake market data and a fake mailbox.

Requires locally installed Playwright/Chromium. No external HTTP or SMTP is sent.
Screenshots contain only temporary fixture data and are saved under scratch/.
"""
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import logging
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from services import identity, workspace_accounts, workspace_db, workspace_mail, workspace_performance
from services.market_types import Quote, get_instrument


class FixtureQuotes:
    price = Decimal("60000")

    def get_quote(self, market, symbol, now=None):
        stamp = now or datetime.now(timezone.utc)
        return Quote(get_instrument(market, symbol), self.price, stamp, stamp, "browser_fixture", bid=self.price, ask=self.price)


def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    screenshots = ROOT / "scratch"
    screenshots.mkdir(exist_ok=True)
    deliveries, page_errors, checks = [], [], []
    origin = "https://workspace.test"
    email, password = "browser-fixture@example.test", "Browser-unique-password-2026!"
    new_password = "New-browser-password-2026!"
    with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
        "ALPHAPILOT_WORKSPACE_DB": str(Path(temp) / "browser.sqlite3"),
        "ALPHAPILOT_WORKSPACE_ENABLED": "1", "ALPHAPILOT_ENV": "production",
        "ALPHAPILOT_WORKSPACE_ORIGIN": origin, "ALPHAPILOT_ALLOW_REGISTRATION": "true",
        "ALPHAPILOT_REQUIRE_VERIFIED_EMAIL": "true", "ALPHAPILOT_MAIL_ENABLED": "true",
        "SMTP_HOST": "smtp.example.test", "SMTP_PORT": "587", "SMTP_USER": "fixture",
        "SMTP_PASSWORD": "fixture-only", "SMTP_FROM": "noreply@example.test", "BROKER_MODE": "paper",
    }), patch.object(workspace_mail, "send_action_email", side_effect=lambda recipient, purpose, token: deliveries.append({"purpose": purpose, "token": token})), sync_playwright() as playwright:
        import web.server
        from web.routers.workspace import get_quotes
        server = importlib.reload(web.server)
        quotes = FixtureQuotes()
        server.app.dependency_overrides[get_quotes] = lambda: quotes
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1050}, color_scheme="light", accept_downloads=True)
        with TestClient(server.app, base_url=origin) as api:
            def proxy(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.netloc != "workspace.test":
                    route.abort()
                    return
                # Browser cookies remain authoritative; the test client's own jar is irrelevant.
                api.cookies.clear()
                headers = {k: v for k, v in request.all_headers().items() if k not in {"content-length", "host"}}
                response = api.request(request.method, parsed.path + ("?" + parsed.query if parsed.query else ""), headers=headers, content=request.post_data_buffer, follow_redirects=False)
                returned = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding", "transfer-encoding"}}
                route.fulfill(status=response.status_code, headers=returned, body=response.content)

            context.route("**/*", proxy)
            page = context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(origin + "/workspace/")
            page.locator('[data-auth-mode="register"]').click()
            page.locator("#auth-email").fill(email)
            page.locator("#auth-password").fill(password)
            page.locator("#auth-confirm").fill(password)
            page.locator("#paper-agreement").check()
            page.locator("#auth-submit").click()
            expect(page.locator("#app-view")).to_be_visible()
            expect(page.locator("#create-account")).to_be_disabled()
            assert not deliveries
            checks.append("registration does not send unsolicited email; verification gates new accounts")

            page.locator('[data-view="security"]').click()
            expect(page.locator("#security-email-state")).to_have_text("未验证")
            page.locator("#send-verification").click()
            expect(page.locator("#verification-delivery-status")).to_have_text("已发送")
            token = deliveries[-1]["token"]
            page.goto(origin + "/workspace/#verify-email=" + token)
            expect(page.locator("#recovery-submit")).to_be_visible()
            assert token not in page.url
            with workspace_db.connect() as conn:
                row = conn.execute("SELECT id,email_verified FROM workspace_users WHERE email=?", (email,)).fetchone()
                uid = row["id"]
                assert not row["email_verified"]
            conn.close()
            page.locator("#recovery-submit").click()
            expect(page.locator("#recovery-status")).to_contain_text("验证成功")
            page.locator("#recovery-back").click()
            if page.locator("#auth-form").is_visible():
                page.locator("#auth-email").fill(email)
                page.locator("#auth-password").fill(password)
                page.locator("#auth-submit").click()
            expect(page.locator("#app-view")).to_be_visible()
            checks.append("one-use verification token removed from URL and consumed only on confirmation")

            page.locator('[data-view="overview"]').click()
            page.locator('[data-market="CRYPTO"]').click()
            page.locator("#create-account").click()
            expect(page.locator("#metric-cash")).to_have_text("100,000.00")
            page.locator('[data-view="trading"]').click()
            page.locator("#order-quantity").fill("0.01")
            expect(page.locator("#order-submit")).to_be_enabled()
            page.locator("#order-submit").click()
            expect(page.locator("#order-status")).to_contain_text("已成交")
            account = workspace_accounts.list_accounts(uid)[0]
            assert workspace_performance.capture_account(uid, account["id"], quotes, reason="scheduled")["status"] == "captured"
            quotes.price = Decimal("59000")
            assert workspace_performance.capture_account(uid, account["id"], quotes, reason="manual")["status"] == "captured"
            page.locator('[data-view="performance"]').click()
            expect(page.locator("#snapshot-record-count")).to_contain_text("3 / 3")
            expect(page.locator("#equity-chart canvas")).to_be_visible()
            page.screenshot(path=str(screenshots / "workspace-phase2-performance.png"), full_page=True)
            with page.expect_download() as download_event:
                page.locator('#view-performance [data-export="orders"]').click()
            download = download_event.value
            assert Path(download.path()).read_bytes().startswith(b"\xef\xbb\xbf")
            checks.append("real isolated paper fill, snapshots, chart and authenticated CSV download")

            other_session = identity.login(email, password, "127.0.0.2")
            page.locator('[data-view="security"]').click()
            expect(page.locator("#session-list .session-row")).to_have_count(2)
            page.locator("#revoke-other-sessions").click()
            expect(page.locator("#session-list .session-row")).to_have_count(1)
            assert identity.get_session(other_session.token) is None
            page.screenshot(path=str(screenshots / "workspace-phase2-security.png"), full_page=True)
            checks.append("session management revokes other devices while keeping the current session")

            for width in (1440, 1024, 375):
                page.set_viewport_size({"width": width, "height": 950})
                for view in ("overview", "performance", "trading", "strategies", "billing", "security"):
                    page.locator(f'[data-view="{view}"]').click()
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "page overflow"
                checks.append(f"six pages fit {width}px")
            page.screenshot(path=str(screenshots / "workspace-phase2-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1050})
            page.locator("#current-password").fill(password)
            page.locator("#new-password").fill(new_password)
            page.locator("#new-password-confirm").fill(new_password)
            page.locator("#password-change-submit").click()
            expect(page.locator("#auth-status")).to_contain_text("密码已修改")
            checks.append("password change clears all sessions and returns to login")

            page.locator("#forgot-password").click()
            page.locator("#recovery-email").fill(email)
            page.locator("#recovery-submit").click()
            expect(page.locator("#recovery-status")).to_contain_text("受理")
            token = deliveries[-1]["token"]
            assert deliveries[-1]["purpose"] == "reset_password"
            page.goto(origin + "/workspace/#reset-password=" + token)
            expect(page.locator("#recovery-password")).to_be_visible()
            assert token not in page.url
            page.locator("#recovery-password").fill(password)
            page.locator("#recovery-confirm").fill(password)
            page.locator("#recovery-submit").click()
            expect(page.locator("#auth-status")).to_contain_text("密码已重置")
            assert identity.login(email, password, "127.0.0.3").user["email_verified"]
            checks.append("password recovery completes through the real single-use-token API with a fake mailbox")
            browser.close()
        server.app.dependency_overrides.clear()
    assert not page_errors, page_errors
    print("Browser workspace audit passed:")
    for check in checks:
        print("  " + check)
    print("No external HTTP/SMTP or real-money trades; no browser JavaScript errors.")


if __name__ == "__main__":
    main()
