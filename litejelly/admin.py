"""Access control for the admin page.

The library API is deliberately open so any TV on the network can browse it.
The admin API is not: it decides which directories the server exposes, so a
request that can change it can make the server serve any file on the machine.
It therefore requires a signed-in admin account.
"""

from __future__ import annotations

import http.cookies
import ipaddress
import logging

from .auth import COOKIE_NAME

log = logging.getLogger("litejelly.admin")

CSRF_HEADER = "X-LiteJelly-Admin"


def is_loopback(address: str) -> bool:
    if not address:
        return False
    text = address.strip()
    if text.startswith("::ffff:"):
        text = text[7:]
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def session_token(headers) -> str:
    """Read the admin session cookie, ignoring anything malformed."""
    if headers is None:
        return ""
    raw = headers.get("Cookie")
    if not raw:
        return ""
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(raw)
    except http.cookies.CookieError:
        return ""
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel else ""


def build_cookie(token: str, max_age: int) -> str:
    # No Secure flag: this serves plain HTTP on a LAN, and setting it would
    # stop the cookie being sent at all. SameSite=Strict is the CSRF defence.
    return "; ".join([
        f"{COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        f"Max-Age={max_age}",
    ])


def clear_cookie() -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"


def same_origin(headers, host: str) -> bool:
    """Reject cross-site writes.

    A browser will not let another site set a custom header without a CORS
    preflight, and it always sends Origin on cross-site POSTs, so checking
    both covers form posts and scripted requests.
    """
    if headers is None:
        return False
    origin = headers.get("Origin")
    if origin:
        expected_host = (host or "").strip()
        try:
            origin_host = origin.split("//", 1)[1]
        except IndexError:
            return False
        if origin_host != expected_host:
            log.warning("Rejected admin write from foreign origin %s", origin)
            return False
        return True
    # No Origin header: require the custom header a same-origin fetch can set.
    return bool(headers.get(CSRF_HEADER))
