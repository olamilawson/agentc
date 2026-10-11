"""Owner sign-in: password, one-time code, session, lockout, and what stays open."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from personal_agent import auth
from personal_agent.api.main import create_app
from personal_agent.harness.models import RecordedGateway

# Test values, made here and used nowhere else.
PASSWORD = "a test passphrase, not a real one"
TOTP = auth.new_totp_secret()


def _code(offset: int = 0) -> str:
    return auth.totp_code(TOTP, int(time.time() // 30) + offset)


@pytest.fixture
def client(settings) -> TestClient:
    settings.owner_password_hash = auth.hash_password(PASSWORD)
    settings.totp_secret = TOTP
    settings.session_secret = "test-session-secret"
    settings.n8n_secret = "hook-secret"
    return TestClient(create_app(settings, gateway=RecordedGateway({})), follow_redirects=False)


def _sign_in(client: TestClient, password: str = PASSWORD, code: str | None = None):
    return client.post("/login", data={"password": password, "code": code or _code(), "next": "/cost"})


def test_every_screen_and_endpoint_needs_a_session(client):
    for path in ("/queue", "/inspect", "/files", "/library", "/cost"):
        page = client.get(path, headers={"accept": "text/html"})
        assert page.status_code == 303 and page.headers["location"].startswith("/login?next=")
    for path in ("/runs", "/clients", "/costs", "/ledger", "/skills", "/approvals/pending"):
        assert client.get(path).status_code == 401
    assert client.post("/runs", json={"workflow": "foundation-test", "client": "acme", "inputs": {}}).status_code == 401
    assert client.post("/clients/acme/delete", data={"confirm": "acme"}).status_code == 401
    # Open on purpose: the health check, the sign-in page, and the watcher's hook behind its own secret.
    assert client.get("/health").json() == {"ok": True}
    assert "Sign in" in client.get("/login").text
    assert client.post("/hooks/n8n/sponsorship", json={}).status_code in (401, 422)
    # The sign-in page needs its fonts; nothing else under /static is open.
    font = client.get("/static/fonts/inter-latin-wght-normal.woff2")
    assert font.status_code == 200 and font.content[:4] == b"wOF2"
    assert client.get("/static/anything-else.txt").status_code == 401


def test_right_password_and_code_open_a_session(client):
    ok = _sign_in(client)
    assert ok.status_code == 303 and ok.headers["location"] == "/cost"
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/queue", headers={"accept": "text/html"}).status_code == 200
    assert "Sign out" in client.get("/queue").text
    assert client.get("/runs").status_code == 200

    client.post("/logout")
    assert client.get("/runs").status_code == 401


def test_wrong_password_or_wrong_code_is_refused_with_one_message(client):
    for attempt in (_sign_in(client, password="wrong"), _sign_in(client, code="000000")):
        assert attempt.status_code == 401
        assert "The password or the code did not match." in attempt.text
        assert "set-cookie" not in attempt.headers
    assert client.get("/runs").status_code == 401


def test_a_code_works_once(client):
    code = _code()
    assert _sign_in(client, code=code).status_code == 303
    client.cookies.clear()
    assert _sign_in(client, code=code).status_code == 401


def test_five_failures_lock_sign_in_even_for_the_right_password(client):
    for _ in range(5):
        _sign_in(client, password="wrong")
    locked = _sign_in(client)
    assert locked.status_code == 429 and "locked sign-in" in locked.text
    assert client.get("/runs").status_code == 401


def test_a_forged_or_expired_cookie_is_refused(client):
    guard = auth.LoginGuard(auth.hash_password(PASSWORD), TOTP, "test-session-secret")
    assert guard.valid_session(guard.new_session())
    assert not guard.valid_session(guard.new_session(now=time.time() - 13 * 3600))  # past its twelve hours
    other = auth.LoginGuard(auth.hash_password(PASSWORD), TOTP, "another-secret")
    client.cookies.set(auth.SESSION_COOKIE, other.new_session())
    assert client.get("/runs").status_code == 401


def test_a_signed_in_browser_cannot_be_driven_from_another_site(client):
    _sign_in(client)
    forged = client.post("/retention/apply", headers={"origin": "https://elsewhere.example"})
    assert forged.status_code == 403
    assert client.post("/retention/apply", headers={"origin": "http://testserver"}).status_code == 200


def test_sign_in_never_redirects_off_the_site(client):
    away = client.post("/login", data={"password": PASSWORD, "code": _code(), "next": "//evil.example/x"})
    assert away.headers["location"] == "/queue"


def test_a_server_without_a_login_refuses_instead_of_serving_open(settings):
    settings.require_login = True
    client = TestClient(create_app(settings, gateway=RecordedGateway({})))
    assert client.get("/queue").status_code == 503
    assert "Sign-in is not set up" in client.get("/runs").text
    assert client.get("/health").status_code == 200


def test_pages_use_the_bundled_fonts_and_call_no_other_host(client):
    from pathlib import Path

    import personal_agent.api.ui as ui

    fonts = Path(ui.__file__).parent / "static" / "fonts"
    for family in ("inter", "playfair-display"):
        for subset in ("latin", "latin-ext"):
            name = f"{family}-{subset}-wght-normal.woff2"
            assert (fonts / name).read_bytes()[:4] == b"wOF2"
            assert f"/static/fonts/{name}" in ui.CSS
    assert (fonts / "Inter-LICENSE.txt").exists() and (fonts / "PlayfairDisplay-LICENSE.txt").exists()
    page = client.get("/login").text
    assert "http://" not in page and "https://" not in page  # no font host, no outside request at all


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
