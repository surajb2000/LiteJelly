"""Access control for the admin page.

The library API is deliberately open so any TV on the network can browse it.
The admin API is not: it decides which directories the server exposes, so a
request that can change it can make the server serve any file on the machine.
Access is therefore limited to the local machine unless an explicit token is
configured.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging

log = logging.getLogger("litejelly.admin")

TOKEN_HEADER = "X-Admin-Token"
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


def _presented_token(headers, query) -> str:
    if headers is not None:
        value = headers.get(TOKEN_HEADER)
        if value:
            return value.strip()
    if query:
        values = query.get("token")
        if values:
            return str(values[0]).strip()
    return ""


def authorize(config, client_address: str, headers=None, query=None) -> tuple[bool, str]:
    """Return (allowed, reason). Reason is only meaningful when denied."""
    configured = (getattr(config, "admin_token", "") or "").strip()
    if configured:
        presented = _presented_token(headers, query)
        if presented and hmac.compare_digest(presented, configured):
            return True, ""
        if is_loopback(client_address):
            return True, ""
        return False, "A valid admin token is required."

    if is_loopback(client_address):
        return True, ""
    return False, ("Admin access is limited to the machine running LiteJelly. "
                   "Set \"admin_token\" in config.json to allow it from other devices.")


def same_origin(headers, host: str) -> bool:
    """Reject cross-site writes.

    A browser will not let another site set a custom header without a CORS
    preflight, and it always sends Origin on cross-site POSTs, so checking
    both covers form posts and scripted requests without needing cookies.
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
