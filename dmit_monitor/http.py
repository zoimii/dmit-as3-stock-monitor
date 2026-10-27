"""Bounded HTTPS transport. Never include URLs, response bodies or tokens in errors."""
import http.cookiejar
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone


class SafeError(Exception):
    pass


@dataclass
class Response:
    status: int
    url: str
    body: str
    headers: dict

    def retry_after(self):
        value = self.headers.get("retry-after", "")
        try:
            return min(86400, max(0, int(value)))
        except ValueError:
            try:
                return min(86400, max(0, int((parsedate_to_datetime(value) -
                    datetime.now(timezone.utc)).total_seconds())))
            except (ValueError, TypeError, OverflowError):
                return 0


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlparse(newurl)
        if (target.scheme != "https" or target.hostname not in self.hosts or
                target.port not in (None, 443) or target.username or target.password):
            raise SafeError("redirect_outside_allowed_origin")
        # POST redirects are deliberately not followed: no accidental order submission.
        if req.get_method() != "GET":
            raise SafeError("post_redirect_requires_review")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Transport:
    def __init__(self, hosts):
        self.hosts = frozenset(hosts)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            RestrictedRedirect(self.hosts),
        )

    def request(self, method, url, data=None, headers=None):
        target = urllib.parse.urlparse(url)
        if (target.scheme != "https" or target.hostname not in self.hosts or
                target.port not in (None, 443) or target.username or target.password):
            raise SafeError("request_outside_allowed_origin")
        request_headers = {
            "User-Agent": "DMIT-Inventory-Monitor/1.0 (official-cart-verification)",
            "Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/json",
            "Cache-Control": "no-cache", **(headers or {}),
        }
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            try:
                result = self.opener.open(req, timeout=15)
            except urllib.error.HTTPError as exc:
                result = exc
            with result:
                body = result.read(2_000_001)
                if len(body) > 2_000_000:
                    raise SafeError("response_too_large")
                return Response(result.code, result.url, body.decode("utf-8", "replace"),
                                {k.lower(): v for k, v in result.headers.items()})
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise SafeError("network_or_tls_error") from None
