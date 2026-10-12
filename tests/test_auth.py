"""Owner sign-in: email and password, session, lockout, what stays open, and the optional code."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from personal_agent import auth
from personal_agent.api.main import create_app
from personal_agent.harness.models import RecordedGateway

# Test values, made here and used nowhere else.
EMAIL = "owner@company.example"
PASSWORD = "a test passphrase, not a real one"
TOTP = auth.new_totp_secret()


def _code(offset: int = 0) -> str:
    return auth.totp_code(TOTP, int(time.time() // 30) + offset)


def _app(settings, with_code: bool = False) -> TestClient:
    settings.owner_email = EMAIL
    settings.owner_password_hash = auth.hash_password(PASSWORD)
    settings.session_secret = "test-session-secret"
    settings.totp_secret = TOTP if with_code else None
    settings.n8n_secret = "hook-secret"
    return TestClient(create_app(settings, gateway=RecordedGateway({})), follow_redirects=False)


@pytest.fixture
def client(settings) -> TestClient:
    return _app(settings)


def _sign_in(client: TestClient, email: str = EMAIL, password: str = PASSWORD, **more):
    return client.post("/login", data={"email": email, "password": password, "next": "/cost", **more})


def test_every_screen_and_endpoint_needs_a_session(client):
    for path in ("/", "/queue", "/inspect", "/calendar", "/files", "/library", "/cost"):
        page = client.get(path, headers={"accept": "text/html"})
        assert page.status_code == 303 and page.headers["location"].startswith("/login?next=")
    for path in ("/runs", "/clients", "/costs", "/ledger", "/skills", "/calls", "/approvals/pending"):
        assert client.get(path).status_code == 401
    assert client.post("/runs", json={"workflow": "foundation-test", "client": "acme", "inputs": {}}).status_code == 401
    assert client.post("/clients/acme/delete", data={"confirm": "acme"}).status_code == 401
    # Open on purpose: the health check, the sign-in page, and the watcher's hook behind its own secret.
    assert client.get("/health").json() == {"ok": True}
    assert client.post("/hooks/n8n/sponsorship", json={}).status_code in (401, 422)
    # The sign-in page needs its fonts; nothing else under /static is open.
    font = client.get("/static/fonts/newsreader-latin-wght-normal.woff2")
    assert font.status_code == 200 and font.content[:4] == b"wOF2"
    assert client.get("/static/anything-else.txt").status_code == 401


def test_sign_in_page_asks_for_email_and_password_and_no_code(client):
    page = client.get("/login").text
    assert 'name="email"' in page and 'name="password"' in page
    assert 'name="code"' not in page and "authenticator" not in page


def test_email_and_password_open_a_session(client):
    ok = _sign_in(client, email="Owner@Company.example ")  # case and stray spaces in the email do not matter
    assert ok.status_code == 303 and ok.headers["location"] == "/cost"
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/queue", headers={"accept": "text/html"}).status_code == 200
    assert "Sign out" in client.get("/queue").text
    assert client.get("/runs").status_code == 200

    client.post("/logout")
    assert client.get("/runs").status_code == 401


def test_wrong_email_or_wrong_password_is_refused_with_one_message(client):
    for attempt in (_sign_in(client, email="someone@else.example"), _sign_in(client, password="wrong")):
        assert attempt.status_code == 401
        assert "The email or the password did not match." in attempt.text
        assert "set-cookie" not in attempt.headers
    assert client.get("/runs").status_code == 401


def test_five_failures_lock_sign_in_even_for_the_right_password(client):
    for _ in range(5):
        _sign_in(client, password="wrong")
    locked = _sign_in(client)
    assert locked.status_code == 429 and "locked sign-in" in locked.text
    assert client.get("/runs").status_code == 401


def test_a_forged_or_expired_cookie_is_refused(client):
    guard = auth.LoginGuard(EMAIL, auth.hash_password(PASSWORD), "test-session-secret")
    assert guard.valid_session(guard.new_session())
    assert not guard.valid_session(guard.new_session(now=time.time() - 13 * 3600))  # past its twelve hours
    other = auth.LoginGuard(EMAIL, auth.hash_password(PASSWORD), "another-secret")
    client.cookies.set(auth.SESSION_COOKIE, other.new_session())
    assert client.get("/runs").status_code == 401


def test_a_signed_in_browser_cannot_be_driven_from_another_site(client):
    _sign_in(client)
    forged = client.post("/retention/apply", headers={"origin": "https://elsewhere.example"})
    assert forged.status_code == 403
    assert client.post("/retention/apply", headers={"origin": "http://testserver"}).status_code == 200


def test_sign_in_never_redirects_off_the_site(client):
    away = client.post("/login", data={"email": EMAIL, "password": PASSWORD, "next": "//evil.example/x"})
    assert away.headers["location"] == "/"


def test_a_server_without_a_sign_in_refuses_instead_of_serving_open(settings):
    settings.require_login = True
    client = TestClient(create_app(settings, gateway=RecordedGateway({})))
    assert client.get("/queue").status_code == 503
    assert "Sign-in is not set up" in client.get("/runs").text
    assert client.get("/health").status_code == 200


def test_pages_use_the_bundled_fonts_and_call_no_other_host(client):
    from pathlib import Path

    import personal_agent.api.ui as ui

    fonts = Path(ui.__file__).parent / "static" / "fonts"
    for family in ("newsreader", "plus-jakarta-sans"):
        for subset in ("latin", "latin-ext"):
            name = f"{family}-{subset}-wght-normal.woff2"
            assert (fonts / name).read_bytes()[:4] == b"wOF2"
            assert f"/static/fonts/{name}" in ui.CSS
    assert (fonts / "Newsreader-LICENSE.txt").exists() and (fonts / "PlusJakartaSans-LICENSE.txt").exists()
    page = client.get("/login").text
    assert "http://" not in page and "https://" not in page  # no font host, no outside request at all


# --- the optional one-time code ---------------------------------------------------------
def test_with_a_code_secret_set_the_page_asks_for_the_code_and_sign_in_needs_it(settings):
    client = _app(settings, with_code=True)
    assert 'name="code"' in client.get("/login").text
    assert _sign_in(client).status_code == 401  # email and password alone are not enough
    assert _sign_in(client, code="000000").status_code == 401
    code = _code()
    assert _sign_in(client, code=code).status_code == 303
    client.cookies.clear()
    assert _sign_in(client, code=code).status_code == 401  # a code works once


def test_setup_link_can_be_drawn_as_a_qr_code_to_scan():
    drawn = auth.qr_text(auth.provisioning_uri(TOTP, "owner"))
    assert len(drawn.splitlines()) > 10 and TOTP not in drawn  # a picture, with the key not spelled out in it


def test_password_hash_and_codes():
    stored = auth.hash_password(PASSWORD)
    assert stored.startswith("scrypt:") and "$" not in stored and PASSWORD not in stored
    assert auth.verify_password(PASSWORD, stored) and not auth.verify_password("other", stored)
    assert not auth.verify_password(PASSWORD, "not a hash")
    # RFC 6238 test vector: the ASCII secret 12345678901234567890 at 59 seconds.
    rfc_secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert auth.totp_code(rfc_secret, 59 // 30) == "287082"
    assert auth.matching_counter(rfc_secret, "287082", now=59) == 1
    assert auth.matching_counter(rfc_secret, "287082", now=59 + 120) is None
