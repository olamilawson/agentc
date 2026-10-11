"""Single-owner login with two-factor (PRD: proxy and login).

One owner, one password, one authenticator app. The password is stored as a
scrypt hash, the second factor is a time-based one-time code (RFC 6238), and a
signed cookie carries the session. All three secrets come from the server
environment; none is in the repository or the database.

The owner creates them on the server:

    python -m personal_agent.auth

That asks for a password, then prints the lines for the server's .env file and
the setup key for an authenticator app.
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
    """Checks a login and signs sessions. Holds the lockout and the last code
    used in memory: one process serves one owner."""

    password_hash: str
    totp_secret: str
    session_secret: str
    session_hours: int = 12
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0
    last_counter: int = -1

    def locked(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.locked_until

    def check(self, password: str, code: str, now: float | None = None) -> bool:
        """True for the right password and a fresh code. Five wrong attempts in
        fifteen minutes lock the login for fifteen minutes."""
        now = now if now is not None else time.time()
        if self.locked(now):
            return False
        password_ok = verify_password(password, self.password_hash)  # both checks run on every attempt
        counter = matching_counter(self.totp_secret, code, now)
        if password_ok and counter is not None and counter > self.last_counter:
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


def main() -> int:
    import getpass

    print("This sets the owner's login. Nothing is saved: copy the lines below into the server's .env file.")
    password = getpass.getpass("Choose a password (12 characters or more): ")
    if len(password) < 12:
        print("That password has fewer than 12 characters. Nothing was created.")
        return 1
    if getpass.getpass("Type it again: ") != password:
        print("The two entries differ. Nothing was created.")
        return 1
    secret = new_totp_secret()
    print()
    print(f"PERSONAL_AGENT_OWNER_PASSWORD_HASH={hash_password(password)}")
    print(f"PERSONAL_AGENT_TOTP_SECRET={secret}")
    print(f"PERSONAL_AGENT_SESSION_SECRET={secrets.token_urlsafe(48)}")
    print()
    print("Add this account to your authenticator app. Enter the setup key by hand, or paste the link:")
    print(f"  setup key: {secret}")
    print(f"  link:      {provisioning_uri(secret, 'owner')}")
    print()
    print("Keep the setup key somewhere safe. With a lost phone and no key, you set the login again from the server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
