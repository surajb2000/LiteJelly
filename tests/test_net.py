"""Outbound connections cannot reach private addresses or leak redirected credentials."""

from __future__ import annotations

import socket
import unittest
import urllib.error
import urllib.request
from unittest import mock

from litejelly.net import CheckedRedirects, PublicHTTPSConnection, _connect_public, validate_remote_url


class URLTests(unittest.TestCase):
    def test_unsafe_urls_are_rejected(self):
        for url in ("file:///tmp/file", "http://example.org", "https://127.0.0.1/",
                    "https://[::1]/", "https://10.0.0.1/", "https://user:pass@example.org/",
                    "https://example.org:8000/", "https://example.org/\nheader"):
            with self.subTest(url=url):
                with self.assertRaises(urllib.error.URLError):
                    validate_remote_url(url)

    def test_provider_hosts_cannot_be_spoofed_by_a_suffix(self):
        allowed = ("opensubtitles.com",)
        self.assertEqual(validate_remote_url("https://dl.opensubtitles.com/file", allowed),
                         "dl.opensubtitles.com")
        with self.assertRaises(urllib.error.URLError):
            validate_remote_url("https://opensubtitles.com.attacker.test/file", allowed)

    def test_redirects_cannot_downgrade_or_cross_credential_boundaries(self):
        redirects = CheckedRedirects(("opensubtitles.com",))
        request = urllib.request.Request("https://api.opensubtitles.com/api/v1/subtitles",
                                         headers={"Api-Key": "fixture"})
        for destination in ("http://api.opensubtitles.com/subtitles",
                            "https://dl.opensubtitles.com/subtitles", "https://127.0.0.1/"):
            with self.subTest(destination=destination):
                with self.assertRaises(urllib.error.URLError):
                    redirects.redirect_request(request, None, 302, "Found", {}, destination)


class ConnectionTests(unittest.TestCase):
    def test_dns_answers_are_checked_before_connecting(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fd00::1"):
            answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            with mock.patch("litejelly.net.socket.getaddrinfo", return_value=answer), \
                    mock.patch("litejelly.net.socket.socket") as connect:
                with self.assertRaises(OSError):
                    _connect_public(("dl.opensubtitles.com", 443), 1)
                connect.assert_not_called()

    def test_connection_uses_the_validated_ip_without_resolving_again(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with mock.patch("litejelly.net.socket.getaddrinfo", return_value=answer) as lookup, \
                mock.patch("litejelly.net.socket.socket") as factory:
            connection = _connect_public(("example.org", 443), 1)
            connection.connect.assert_called_once_with(("8.8.8.8", 443))
            self.assertIs(connection, factory.return_value)
            lookup.assert_called_once()

    def test_tls_still_verifies_the_original_hostname(self):
        connection = PublicHTTPSConnection("example.org")
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection.host, "example.org")
        self.assertIs(connection._create_connection, _connect_public)


if __name__ == "__main__":
    unittest.main()
