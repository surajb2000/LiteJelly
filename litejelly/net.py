"""HTTPS-only outbound requests with redirect checks and public-address pinning."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request


def validate_remote_url(url: str, allowed_hosts: tuple[str, ...] = ()) -> str:
    """Return a permitted hostname; reject local literals, credentials and unsafe schemes."""
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (any(ord(char) < 32 for char in url) or parsed.scheme != "https"
                or not hostname or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            raise ValueError("Unsafe remote URL")
        if allowed_hosts and not any(hostname == host or hostname.endswith("." + host)
                                     for host in allowed_hosts):
            raise ValueError("Remote host is not allowed")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("Non-public remote address")
        return hostname
    except ValueError as error:
        raise urllib.error.URLError("Remote destination is not allowed") from error


def _connect_public(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """Resolve once, reject every non-public answer, and connect to a validated address."""
    hostname, port = address
    candidates = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    if not candidates or any(not ipaddress.ip_address(item[4][0]).is_global for item in candidates):
        raise OSError("Remote host resolved to a non-public address")
    last_error = None
    for family, kind, protocol, _canonical, endpoint in candidates:
        connection = socket.socket(family, kind, protocol)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(endpoint)
            return connection
        except OSError as error:
            last_error = error
            connection.close()
    raise last_error or OSError("No usable remote address")


class PublicHTTPSConnection(http.client.HTTPSConnection):
    """Pin the socket address while retaining HTTPSConnection's hostname/TLS checks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        """Use the checked connection factory without disabling certificate verification."""
        return self.do_open(PublicHTTPSConnection, request, context=self._context)


class CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...]):
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, request, response, code, message, headers, newurl):
        """Validate every redirect and prevent credentials from crossing host boundaries."""
        hostname = validate_remote_url(newurl, self.allowed_hosts)
        sensitive = any(name.lower() in {"authorization", "api-key"}
                        for name, _value in request.header_items())
        if sensitive and hostname != validate_remote_url(request.full_url, self.allowed_hosts):
            raise urllib.error.URLError("Credentialed redirects must stay on the same host")
        return super().redirect_request(request, response, code, message, headers, newurl)


def open_remote(request, timeout: float = 20, allowed_hosts: tuple[str, ...] = ()):
    """Open a direct public HTTPS connection without environment proxy redirection."""
    validate_remote_url(request.full_url, allowed_hosts)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        PublicHTTPSHandler(), CheckedRedirects(allowed_hosts))
    return opener.open(request, timeout=timeout)