"""A small HTTP helper on top of urllib: an endpoint allowlist, no redirects, timeouts.

Credentials may only travel to the official API host of each platform over
HTTPS. The offline test suite also needs a loopback address (its local mock
servers); that is allowed only when the caller says so, which the command line
does only when ``ADOPS_GUARD_TEST_ENDPOINTS=1`` is set. Redirects are never
followed, so a misbehaving endpoint cannot bounce a request, with its
``Authorization`` header, to another host.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from adops_guard import __version__
from adops_guard.errors import ConfigError, NetworkError
from adops_guard.hints import NETWORK_HINT
from adops_guard.redact import redact

USER_AGENT = f"adops-guard/{__version__}"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Return redirects to the caller as plain 3xx responses instead of following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_OPENER = urllib.request.build_opener(_NoRedirects())


def check_endpoint(url: str, official_hosts: tuple[str, ...], *, allow_loopback: bool = False) -> str:
    """Return ``url`` (without a trailing slash) if credentials may be sent there, else raise.

    ``allow_loopback`` admits ``http(s)://127.0.0.1``, ``[::1]`` and ``localhost``
    for the offline tests; it is never enabled by a config file alone.
    """
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.username or parts.password:
        raise ConfigError("endpoint URLs must not contain user names or passwords")
    if host in LOOPBACK_HOSTS:
        if not allow_loopback:
            raise ConfigError(
                f"refusing to send credentials to the loopback address in {url!r}",
                hint="loopback endpoints are for the offline test suite (ADOPS_GUARD_TEST_ENDPOINTS=1)",
            )
        if parts.scheme not in ("http", "https"):
            raise ConfigError(f"unsupported URL scheme in {url!r}")
        return url.rstrip("/")
    if parts.scheme != "https":
        raise ConfigError(f"refusing a non-HTTPS endpoint: {url!r}")
    if host not in official_hosts:
        raise ConfigError(
            f"refusing to send credentials to {host or url!r}",
            hint="only the official API host (" + ", ".join(official_hosts) + ") is allowed",
        )
    return url.rstrip("/")


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self.body.strip():
            return {}
        return json.loads(self.body.decode("utf-8"))

    def json_or_none(self) -> Any:
        try:
            return self.json()
        except ValueError:
            return None


def _lower(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    return {str(key).lower(): str(value) for key, value in headers.items()}


def safe_url(url: str) -> str:
    """The URL without its query string, for messages."""
    parts = urllib.parse.urlsplit(url)
    return redact(urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 60.0,
) -> Response:
    """Send one request. HTTP error statuses are returned, not raised; network failures raise."""
    all_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    all_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return Response(resp.status, _lower(resp.headers), resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except (OSError, http.client.HTTPException):
            body = b""
        finally:
            exc.close()  # free the connection now, not whenever the error object is garbage-collected
        return Response(exc.code, _lower(exc.headers), body or b"")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise NetworkError(f"network error on {method} {safe_url(url)}: {reason}", hint=NETWORK_HINT) from None
    except http.client.HTTPException as exc:  # e.g. IncompleteRead, BadStatusLine: not OSError subclasses
        raise NetworkError(
            f"network error on {method} {safe_url(url)}: {type(exc).__name__}", hint=NETWORK_HINT
        ) from None
