"""A minimal Meta Graph / Marketing API client using only urllib.

* The access token travels only in the ``Authorization: Bearer`` header. It is
  never added to a URL, and paging uses cursors instead of following
  ``paging.next`` URLs.
* A **usage guard** reads Meta's rate-limit headers (``X-App-Usage``,
  ``X-Ad-Account-Usage``, ``X-Business-Use-Case-Usage``,
  ``X-FB-Ads-Insights-Throttle``) after every response and refuses to send
  another request once the highest value reaches the stop threshold (85 % by
  default). Re-running later continues where it stopped.
* A response with an ``error`` object is an error even when the HTTP status
  is 200 (observed in October 2026).
* Reads and validate-only writes are retried on transient errors (HTTP 5xx,
  ``is_transient``, codes 1 and 2) and network errors. Rate limits are not
  retried: they stop the command (exit 5). Real writes are never retried (a
  5xx or a timeout raises :class:`~adops_guard.errors.AmbiguousWrite`).
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from adops_guard import http
from adops_guard.errors import AmbiguousWrite, ApiError, ConfigError, NetworkError, RateLimited
from adops_guard.hints import META_RATE_CODES, META_RATE_HINT, meta_hint
from adops_guard.redact import Secret

DEFAULT_API_VERSION = "v26.0"
API_HOSTS = ("graph.facebook.com",)
TRANSIENT_CODES = frozenset({1, 2})
RATE_LIMIT_CODES = META_RATE_CODES
USAGE_HEADERS = ("x-app-usage", "x-ad-account-usage", "x-business-use-case-usage", "x-fb-ads-insights-throttle")
PERCENT_KEYS = ("call_count", "total_cputime", "total_time", "acc_id_util_pct", "app_id_util_pct")
REPEAT_HINT = (
    "re-run the same command: it reads the current value first and skips the change if it was "
    "already applied; the journal shows exactly what was sent"
)
_NONEXISTING = re.compile(r"nonexisting field \(([^)]+)\)")
_PATH = re.compile(r"[A-Za-z0-9_./-]+")


def encode_params(params: dict[str, Any]) -> dict[str, str]:
    out = {}
    for key, value in params.items():
        if value is None:
            continue
        out[key] = json.dumps(value) if isinstance(value, (dict, list, tuple, bool)) else str(value)
    return out


def drop_field(fields: str, bad: str) -> str:
    """Remove one top-level field (with any ``{...}`` sub-selection) from a fields list."""
    parts, depth, current = [], 0, ""
    for ch in fields:
        depth += ch == "{"
        depth -= ch == "}"
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return ",".join(p.strip() for p in parts if p.strip() and p.split("{", 1)[0].strip() != bad)


def _percentages(value: Any) -> list[float]:
    found = []
    if isinstance(value, dict):
        for key in PERCENT_KEYS:
            try:
                found.append(float(value[key]))
            except (KeyError, TypeError, ValueError):
                continue
    return found


class MetaClient:
    def __init__(
        self,
        token: Secret,
        *,
        api_version: str = DEFAULT_API_VERSION,
        base_url: str = "https://graph.facebook.com",
        usage_stop: int = 85,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
        allow_test_endpoints: bool = False,
    ) -> None:
        if not re.fullmatch(r"v\d{1,3}\.\d", api_version):
            raise ConfigError(f"meta api_version must look like v26.0 (got {api_version!r})")
        self._token = token
        self.version = api_version
        self.base = http.check_endpoint(base_url, API_HOSTS, allow_loopback=allow_test_endpoints)
        self.usage_stop = usage_stop
        self.timeout = timeout
        self._sleep = sleep
        self.max_attempts = max(1, max_attempts)
        self.usage: dict[str, Any] = {}
        self.calls = 0

    # ------------------------------------------------------------------ usage guard
    def _note_usage(self, headers: dict[str, str]) -> None:
        for name in USAGE_HEADERS:
            raw = headers.get(name)
            if not raw:
                continue
            try:
                self.usage[name] = json.loads(raw)
            except ValueError:
                continue

    def usage_percent(self) -> float:
        values = [0.0]
        for name, value in self.usage.items():
            if name == "x-business-use-case-usage" and isinstance(value, dict):
                for entries in value.values():
                    for entry in entries or []:
                        values += _percentages(entry)
            else:
                values += _percentages(value)
        return max(values)

    def regain_minutes(self) -> int | None:
        buc = self.usage.get("x-business-use-case-usage")
        minutes = []
        if isinstance(buc, dict):
            for entries in buc.values():
                for entry in entries or []:
                    if isinstance(entry, dict):
                        try:
                            minutes.append(int(entry.get("estimated_time_to_regain_access") or 0))
                        except (TypeError, ValueError):
                            continue
        best = max(minutes, default=0)
        return best or None

    def _check_usage(self) -> None:
        percent = self.usage_percent()
        if percent >= self.usage_stop:
            wait = self.regain_minutes()
            raise RateLimited(
                f"Meta API usage is at {percent:.0f}% (this tool stops at {self.usage_stop}%); no request was sent",
                hint=(f"Meta estimates {wait} min until access is fully back; " if wait else "")
                + "the usage window is rolling (up to an hour): re-run the same command later",
            )

    # ------------------------------------------------------------------ transport
    def _url(self, path: str) -> str:
        clean = path.strip().lstrip("/")
        if not _PATH.fullmatch(clean) or ".." in clean:
            raise ConfigError(f"unexpected Graph API path {path!r}")
        return f"{self.base}/{self.version}/{clean}"

    def _error(self, status: int, err: dict[str, Any], where: str, idempotent: bool) -> ApiError:
        code, sub = err.get("code"), err.get("error_subcode")
        parts = [f"{where}: HTTP {status}", f"code {code}" + (f"/{sub}" if sub else "")]
        if err.get("type"):
            parts.append(str(err["type"]))
        parts.append(str(err.get("message") or "no message"))
        if err.get("error_user_title") or err.get("error_user_msg"):
            parts.append(f"{err.get('error_user_title', '')}: {err.get('error_user_msg', '')}".strip(": "))
        if err.get("fbtrace_id"):
            parts.append(f"fbtrace {err['fbtrace_id']}")
        message = " | ".join(parts)
        hint = meta_hint(code, sub)  # a one-line fix for errors we know (docs/troubleshooting.md)
        error: ApiError
        if code in RATE_LIMIT_CODES or status == 429:
            error = RateLimited(
                message,
                status=status,
                code=code,
                subcode=sub,
                hint=hint or META_RATE_HINT,
            )
        elif status >= 500 and not idempotent:
            error = AmbiguousWrite(message, status=status, code=code, subcode=sub, hint=REPEAT_HINT)
        else:
            error = ApiError(message, status=status, code=code, subcode=sub, hint=hint)
        error.transient = bool(err.get("is_transient")) or code in TRANSIENT_CODES or status in (500, 502, 503, 504)
        return error

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None, *, idempotent: bool, where: str
    ) -> dict[str, Any]:
        self._check_usage()
        url = self._url(path)
        data = None
        headers = {"Authorization": f"Bearer {self._token.reveal()}"}
        if method == "GET":
            if params:
                url += "?" + urllib.parse.urlencode(encode_params(params))
        else:
            data = urllib.parse.urlencode(encode_params(params or {})).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        self.calls += 1
        try:
            resp = http.request(method, url, headers=headers, data=data, timeout=self.timeout)
        except NetworkError as exc:
            if not idempotent:
                raise AmbiguousWrite(f"{exc}; the write may or may not have been applied", hint=REPEAT_HINT) from None
            raise
        self._note_usage(resp.headers)
        body = resp.json_or_none()
        err = body.get("error") if isinstance(body, dict) else None
        if resp.status == 200 and not isinstance(err, dict):
            return body if isinstance(body, dict) else {"result": body}
        if not isinstance(err, dict):
            err = {"message": resp.text()[:500]}
        raise self._error(resp.status, err, where, idempotent)

    def _with_retries(self, send: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        attempt = 0
        while True:
            attempt += 1
            try:
                return send()
            except (RateLimited, AmbiguousWrite):
                raise
            except NetworkError:
                if attempt >= self.max_attempts:
                    raise
            except ApiError as exc:
                if not exc.transient or attempt >= self.max_attempts:
                    raise
            self._sleep(min(5.0 * attempt, 30.0))

    # ------------------------------------------------------------------ API
    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._with_retries(lambda: self._request("GET", path, params, idempotent=True, where=f"GET {path}"))

    def get_fields(self, path: str, fields: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET with ``fields``; fields this API version does not know are dropped and listed in ``_dropped``."""
        dropped: list[str] = []
        for _ in range(12):
            try:
                result = self.get(path, {**(params or {}), "fields": fields})
            except RateLimited:
                raise
            except ApiError as exc:
                match = _NONEXISTING.search(exc.message)
                smaller = drop_field(fields, match.group(1)) if match else fields
                if not match or smaller == fields or not smaller:
                    raise
                dropped.append(match.group(1))
                fields = smaller
                continue
            if dropped:
                result["_dropped"] = dropped
            return result
        raise ApiError(f"too many unknown fields on {path}: {', '.join(dropped)}")

    def get_all(self, path: str, params: dict[str, Any] | None = None, cap: int = 5000) -> list[dict[str, Any]]:
        """Follow ``after`` cursors (never ``paging.next`` URLs) up to ``cap`` items."""
        query = dict(params or {})
        query.setdefault("limit", 100)
        items: list[dict[str, Any]] = []
        while True:
            result = self.get(path, query)
            items.extend(result.get("data") or [])
            paging = result.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not paging.get("next") or not after or len(items) >= cap:
                return items[:cap]
            query["after"] = after

    def post(self, path: str, params: dict[str, Any], *, validate_only: bool = False) -> dict[str, Any]:
        body = dict(params)
        if validate_only:
            body["execution_options"] = ["validate_only"]
            where = f"POST {path} (validate only)"
            return self._with_retries(lambda: self._request("POST", path, body, idempotent=True, where=where))
        return self._request("POST", path, body, idempotent=False, where=f"POST {path}")
