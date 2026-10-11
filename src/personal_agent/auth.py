"""Single-owner sign-in (PRD: proxy and login).

One owner signs in with an email address and a password. The password is
stored as a scrypt hash and a signed cookie carries the session. The secrets
come from the server environment; none is in the repository or the database.

A second factor is available and off by default: a time-based one-time code
from an authenticator app (RFC 6238). The PRD lists two-factor; the owner chose
email and password. Setting PERSONAL_AGENT_TOTP_SECRET turns the code on.

The owner creates the sign-in on the server:

    python -m personal_agent.auth

That asks for an email address and a password, then prints the lines for the
server's .env file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass, field

SESSION_COOKIE = "pa_session"
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60


# --- password ----------------------------------------------------------------------
def hash_password(password: str) -> str:
    """scrypt:N:r:p:salt:hash, with no character an .env file treats as special."""
    salt = secrets.token_bytes(16)
    n, r, p = 2**15, 8, 1
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=2**26, dklen=32)
    encode = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")  # noqa: E731
    return f"scrypt:{n}:{r}:{p}:{encode(salt)}:{encode(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split(":")
        decode = lambda text: base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))  # noqa: E731
        expected = decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=decode(salt), n=int(n), r=int(r), p=int(p), maxmem=2**26, dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return scheme == "scrypt" and hmac.compare_digest(actual, expected)


# --- one-time codes (RFC 6238: SHA-1, 30 seconds, 6 digits) -------------------------------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def totp_code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.replace(" ", "").upper())
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def matching_counter(secret: str, code: str, now: float | None = None) -> int | None:
    """The time step a code belongs to, allowing one step of clock drift each way."""
    current = int((now if now is not None else time.time()) // 30)
    code = code.strip().replace(" ", "")
    for counter in (current, current - 1, current + 1):
        try:
            if hmac.compare_digest(totp_code(secret, counter), code):
                return counter
        except (ValueError, TypeError):
            return None
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "Personal Agent") -> str:
    from urllib.parse import quote

    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"


# --- the guard ---------------------------------------------------------------------
@dataclass
class LoginGuard:
    """Checks a sign-in and signs sessions. Holds the lockout in memory: one
    process serves one owner."""

    email: str
    password_hash: str
    session_secret: str
    totp_secret: str | None = None  # set: a one-time code is required as well
    session_hours: int = 12
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0
    last_counter: int = -1

    def locked(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.locked_until

    def check(self, email: str, password: str, code: str = "", now: float | None = None) -> bool:
        """True for the owner's email and password (and a fresh code, if codes are
        on). Five wrong attempts in fifteen minutes lock sign-in for fifteen minutes."""
        now = now if now is not None else time.time()
        if self.locked(now):
            return False
        # Every check runs on every attempt, so a wrong email takes as long as a wrong password.
        email_ok = hmac.compare_digest(email.strip().lower().encode(), self.email.strip().lower().encode())
        password_ok = verify_password(password, self.password_hash)
        code_ok = True
        if self.totp_secret:
            counter = matching_counter(self.totp_secret, code, now)
            code_ok = counter is not None and counter > self.last_counter
        if email_ok and password_ok and code_ok:
            if self.totp_secret:
                self.last_counter = counter  # a code works once
            self.failures.clear()
            return True
        self.failures = [t for t in self.failures if now - t < LOCK_SECONDS] + [now]
        if len(self.failures) >= MAX_FAILURES:
            self.locked_until = now + LOCK_SECONDS
            self.failures.clear()
        return False

    def _sign(self, body: str) -> str:
        return hmac.new(self.session_secret.encode(), body.encode(), hashlib.sha256).hexdigest()

    def new_session(self, now: float | None = None) -> str:
        expires = int((now if now is not None else time.time()) + self.session_hours * 3600)
        body = f"{expires}.{secrets.token_hex(8)}"
        return f"{body}.{self._sign(body)}"

    def valid_session(self, token: str | None, now: float | None = None) -> bool:
        if not token or token.count(".") != 2:
            return False
        body, _, signature = token.rpartition(".")
        if not hmac.compare_digest(self._sign(body), signature):
            return False
        try:
            return int(body.split(".")[0]) > (now if now is not None else time.time())
        except ValueError:
            return False


def qr_text(uri: str) -> str:
    """The setup link as a QR code drawn in text, for a phone to scan off the screen."""
    import io

    import segno

    out = io.StringIO()
    segno.make(uri, error="l").terminal(out=out, compact=True, border=2)
    return out.getvalue()


def main() -> int:
    import getpass
    import sys

    with_code = "--with-code" in sys.argv
    # Prompts go to the terminal even when the printed lines are sent to a file.
    print("Your email address: ", end="", file=sys.stderr, flush=True)
    email = sys.stdin.readline().strip()
    if "@" not in email or " " in email:
        print("That does not look like an email address. Nothing was created.")
        return 1
    password = getpass.getpass("Choose a password (12 characters or more): ")
    if len(password) < 12:
        print("That password has fewer than 12 characters. Nothing was created.")
        return 1
    if getpass.getpass("Type it again: ") != password:
        print("The two entries differ. Nothing was created.")
        return 1
    secret = new_totp_secret() if with_code else ""
    print("Copy these lines into the server's .env file:")
    print()
    print(f"PERSONAL_AGENT_OWNER_EMAIL={email}")
    print(f"PERSONAL_AGENT_OWNER_PASSWORD_HASH={hash_password(password)}")
    print(f"PERSONAL_AGENT_SESSION_SECRET={secrets.token_urlsafe(48)}")
    print(f"PERSONAL_AGENT_TOTP_SECRET={secret}")
    if with_code:
        print()
        print("Add this account to your authenticator app, by scanning the code below or typing the setup key:")
        print(f"  setup key: {secret}")
        print()
        print("Scan this with your authenticator app:")
        print(qr_text(provisioning_uri(secret, email)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
