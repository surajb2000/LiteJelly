"""Admin accounts: password hashing, credential storage and sessions.

The admin API decides which directories the server hands files out of, so this
is the boundary that matters. Passwords are never stored, only a salted
PBKDF2 hash, and the expensive verification happens once at login rather than
on every request: a session cookie carries the result afterwards.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("litejelly.auth")

CREDENTIALS_FILE = "credentials.json"
COOKIE_NAME = "litejelly_admin"

HASH_NAME = "sha256"
# Deliberately lower than OWASP's 600k for SHA-256. The target hardware is a
# phone, and every login attempt costs the media server this much CPU; the
# rate limiter below is what actually stops guessing, not the iteration count.
PBKDF2_ITERATIONS = 210_000

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
MIN_USERNAME_LENGTH = 2
MAX_USERNAME_LENGTH = 32

SESSION_LIFETIME = 7 * 24 * 3600
SESSION_SWEEP_INTERVAL = 3600

# Failed logins allowed from one address before it is locked out for a while.
MAX_FAILURES = 8
LOCKOUT_SECONDS = 900


# -- password hashing -----------------------------------------------------

def hash_password(password: str, *, salt: bytes | None = None,
                  iterations: int = PBKDF2_ITERATIONS) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(HASH_NAME, password.encode("utf-8"), salt, iterations)
    return "$".join([
        f"pbkdf2_{HASH_NAME}",
        str(iterations),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ])


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$")
        if not algorithm.startswith("pbkdf2_"):
            return False
        name = algorithm.split("_", 1)[1]
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        candidate = hashlib.pbkdf2_hmac(name, password.encode("utf-8"),
                                        salt, int(iterations))
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(candidate, expected)


def check_password_strength(password: str) -> list[str]:
    """Length only. Composition rules push people towards worse passwords."""
    errors = []
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        errors.append(f"Password must be {MAX_PASSWORD_LENGTH} characters or fewer")
    return errors


def check_username(username: str) -> list[str]:
    errors = []
    text = (username or "").strip()
    if len(text) < MIN_USERNAME_LENGTH:
        errors.append(f"Username must be at least {MIN_USERNAME_LENGTH} characters")
    elif len(text) > MAX_USERNAME_LENGTH:
        errors.append(f"Username must be {MAX_USERNAME_LENGTH} characters or fewer")
    elif any(ch.isspace() for ch in text):
        errors.append("Username cannot contain spaces")
    return errors


# -- credential storage ---------------------------------------------------

@dataclass
class Credentials:
    username: str
    password_hash: str
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "password_hash": self.password_hash,
            "updated_at": self.updated_at,
        }


def credentials_path(app_dir: Path) -> Path:
    return app_dir / CREDENTIALS_FILE


def load_credentials(app_dir: Path) -> Credentials | None:
    path = credentials_path(app_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Could not read %s: %s", CREDENTIALS_FILE, exc)
        return None
    if not isinstance(data, dict):
        return None
    username = str(data.get("username") or "").strip()
    password_hash = str(data.get("password_hash") or "")
    if not username or not password_hash:
        return None
    try:
        updated = float(data.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated = 0.0
    return Credentials(username, password_hash, updated)


def save_credentials(app_dir: Path, credentials: Credentials) -> None:
    """Write atomically, readable only by the owner where that is supported."""
    path = credentials_path(app_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(credentials.to_dict(), indent=2)

    handle = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows and some filesystems do not support this.


# -- sessions -------------------------------------------------------------

class SessionStore:
    """In-memory sessions. A restart signs everyone out, which is fine."""

    def __init__(self, lifetime: float = SESSION_LIFETIME):
        self.lifetime = lifetime
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.time()

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = (username, time.time() + self.lifetime)
        return token

    def validate(self, token: str | None) -> str | None:
        if not token:
            return None
        self._maybe_sweep()
        now = time.time()
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            username, expires = entry
            if expires < now:
                self._sessions.pop(token, None)
                return None
            # Sliding expiry: active use keeps a session alive.
            self._sessions[token] = (username, now + self.lifetime)
            return username

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _maybe_sweep(self) -> None:
        now = time.time()
        with self._lock:
            if now - self._last_sweep < SESSION_SWEEP_INTERVAL:
                return
            self._last_sweep = now
            expired = [key for key, (_, exp) in self._sessions.items() if exp < now]
            for key in expired:
                self._sessions.pop(key, None)


# -- brute force protection ----------------------------------------------

class LoginThrottle:
    """Locks an address out after repeated failures.

    Password verification is deliberately expensive, so unlimited attempts
    would also be a way to burn the server's CPU, not just to guess.
    """

    def __init__(self, max_failures: int = MAX_FAILURES,
                 lockout: float = LOCKOUT_SECONDS):
        self.max_failures = max_failures
        self.lockout = lockout
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def locked_for(self, address: str) -> float:
        """Seconds remaining, 0 when not locked."""
        with self._lock:
            entry = self._failures.get(address)
            if entry is None:
                return 0.0
            count, last = entry
            if count < self.max_failures:
                return 0.0
            remaining = self.lockout - (time.time() - last)
            if remaining <= 0:
                self._failures.pop(address, None)
                return 0.0
            return remaining

    def record_failure(self, address: str) -> None:
        with self._lock:
            count, _ = self._failures.get(address, (0, 0.0))
            self._failures[address] = (count + 1, time.time())

    def record_success(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)

    def remaining_attempts(self, address: str) -> int:
        with self._lock:
            count, _ = self._failures.get(address, (0, 0.0))
            return max(0, self.max_failures - count)
