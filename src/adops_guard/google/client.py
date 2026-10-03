"""A minimal Google Ads API client over REST, using only urllib.

* OAuth: the refresh token is exchanged for an access token with a POST body
  (never a URL); the access token is cached in memory until shortly before it
  expires and is never written anywhere. A refresh that fails with HTTP 5xx,
  429 or a network error is a temporary :class:`~adops_guard.errors.NetworkError`
  (retried); only a refusal (400/401: revoked or wrong credentials) is a
  :class:`~adops_guard.errors.CredentialError`.
* Reads (GAQL search) and validate-only mutates are retried on 429/5xx and
  network errors. Real writes are not: an HTTP 5xx or a timeout during a write
  raises :class:`~adops_guard.errors.AmbiguousWrite`, because the write may or
  may not have happened. A failed token refresh happens before anything is
  sent, so it is retried and never reported as an ambiguous write.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from adops_guard import http
from adops_guard.credentials import GoogleCredentials
from adops_guard.errors import AmbiguousWrite, ApiError, ConfigError, CredentialError, NetworkError, RateLimited
from adops_guard.hints import GOOGLE_QUOTA_HINT, google_hint, oauth_hint
from adops_guard.redact import REDACTOR

DEFAULT_API_VERSION = "v25"
API_HOSTS = ("googleads.googleapis.com",)
OAUTH_HOSTS = ("oauth2.googleapis.com",)
AUTO_VERSIONS = tuple(f"v{n}" for n in range(30, 18, -1))
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
REPEAT_HINT = (
    "re-run the same command: it reads the current value first and skips the change if it was "
    "already applied; the journal shows exactly what was sent"
)


def normalize_customer_id(value: Any, what: str = "customer id") -> str:
    digits = re.sub(r"[\s-]", "", str(value or ""))
    if not re.fullmatch(r"\d{10}", digits):
        raise ConfigError(f"{what} must have 10 digits, like 123-456-7890 (got {value!r})")
    return digits


def _path(elements: list[dict[str, Any]]) -> str:
    out = ""
    for element in elements:
        name = element.get("fieldName", "?")
        out += ("." if out else "") + name
        if "index" in element:
            out += f"[{element['index']}]"
    return out


def describe_error(resp: http.Response) -> tuple[str, list[str]]:
    """A one-line description of a Google Ads error response, plus its error codes.

    The codes are the values of each ``errorCode`` in a GoogleAdsFailure (``USER_PERMISSION_DENIED``), and the
    ``reason`` of any ``google.rpc.ErrorInfo`` detail (``SERVICE_DISABLED`` when the API is not enabled in the
    Cloud project), which is how Google Cloud reports problems before the Google Ads API itself answers.
    """
    data = resp.json_or_none()
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return f"HTTP {resp.status}: {resp.text()[:300]}", []
    codes: list[str] = []
    parts: list[str] = []
    request_id = None
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        request_id = detail.get("requestId") or request_id
        if isinstance(detail.get("reason"), str):  # google.rpc.ErrorInfo
            codes.append(detail["reason"])
        for err in detail.get("errors") or []:
            code = ", ".join(f"{k}={v}" for k, v in (err.get("errorCode") or {}).items())
            codes += [str(v) for v in (err.get("errorCode") or {}).values()]
            where = _path((err.get("location") or {}).get("fieldPathElements") or [])
            parts.append(f"{code}: {err.get('message', '')}" + (f" (at {where})" if where else ""))
    head = f"HTTP {resp.status} {error.get('status') or ''}".strip()
    text = head + ": " + ("; ".join(parts) if parts else str(error.get("message") or "no message"))
    if request_id:
        text += f" [requestId {request_id}]"
    return text, codes


class GoogleAdsClient:
    def __init__(
        self,
        credentials: GoogleCredentials,
        customer_id: str | None,
        *,
        api_version: str = DEFAULT_API_VERSION,
        api_base_url: str = "https://googleads.googleapis.com",
        oauth_token_url: str = "https://oauth2.googleapis.com/token",
        login_customer_id: str | None = None,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_attempts: int = 3,
        allow_test_endpoints: bool = False,
    ) -> None:
        self._creds = credentials
        self.customer_id = normalize_customer_id(customer_id) if customer_id else None
        login = login_customer_id or credentials.login_customer_id
        self.login_customer_id = normalize_customer_id(login, "login customer id") if login else None
        self.base = http.check_endpoint(api_base_url, API_HOSTS, allow_loopback=allow_test_endpoints)
        self.token_url = http.check_endpoint(oauth_token_url, OAUTH_HOSTS, allow_loopback=allow_test_endpoints)
        if api_version != "auto" and not re.fullmatch(r"v\d{1,3}", api_version):
            raise ConfigError(f"google api_version must look like v25 or be 'auto' (got {api_version!r})")
        self._version = api_version
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self.max_attempts = max(1, max_attempts)
        self._access_token: str | None = None
        self._expires_at = 0.0
        self.cache: dict[str, Any] = {}

    # ------------------------------------------------------------------ auth
    def _token(self) -> str:
        if self._access_token and self._clock() < self._expires_at - 60:
            return self._access_token
        body = urllib.parse.urlencode(
            {
                "client_id": self._creds.client_id.reveal(),
                "client_secret": self._creds.client_secret.reveal(),
                "refresh_token": self._creds.refresh_token.reveal(),
                "grant_type": "refresh_token",
            }
        ).encode()
        resp = http.request(
            "POST",
            self.token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
            timeout=self.timeout,
        )
        if resp.status >= 500 or resp.status == 429:  # Google's side is busy or down: temporary, retry later
            raise NetworkError(
                f"the OAuth token endpoint answered HTTP {resp.status}; retry later",
                hint="Google's token endpoint is busy (not a credentials problem): re-run later",
            )
        data = resp.json_or_none() or {}
        if resp.status != 200 or not isinstance(data, dict) or not data.get("access_token"):
            error = data.get("error") if isinstance(data, dict) else None
            reason = f"{error or ''} {data.get('error_description', '')}".strip() if isinstance(data, dict) else ""
            raise CredentialError(
                f"OAuth token refresh failed: HTTP {resp.status} {reason or resp.text()[:200]}", hint=oauth_hint(error)
            )
        token = str(data["access_token"])
        REDACTOR.register(token)
        self._access_token = token
        self._expires_at = self._clock() + int(data.get("expires_in") or 3600)
        return token

    def authenticate(self) -> None:
        """Exchange the refresh token for an access token now: a read-only check of the OAuth setup."""
        self._token()

    def _headers(self, use_login: bool) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
        if self._creds.developer_token:  # optional since Google's developer-token sunset (September 2026)
            headers["developer-token"] = self._creds.developer_token.reveal()
        if use_login and self.login_customer_id:
            headers["login-customer-id"] = self.login_customer_id
        return headers

    # ------------------------------------------------------------------ transport
    def api_version(self) -> str:
        if self._version != "auto":
            return self._version
        for version in AUTO_VERSIONS:
            resp = http.request(
                "GET", f"{self.base}/{version}/customers:listAccessibleCustomers",
                headers=self._headers(False), timeout=self.timeout,
            )  # fmt: skip
            if resp.status == 404:
                continue
            if resp.status == 200:
                self._version = version
                return version
            message, codes = describe_error(resp)
            raise ApiError(message, status=resp.status, code=codes, hint=google_hint(codes, resp.status))
        raise ApiError("no Google Ads API version between v19 and v30 answered", hint="set google api_version")

    def call(self, method: str, path: str, body: Any = None, *, idempotent: bool, use_login: bool = True) -> Any:
        url = f"{self.base}/{self.api_version()}/{path}"
        data = json.dumps(body).encode() if body is not None else None
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            try:  # a token refresh can fail on the network; nothing has been sent yet, so it is never ambiguous
                headers = self._headers(use_login)
            except NetworkError:
                if attempt < self.max_attempts:
                    self._sleep(min(2**attempt, 30))
                    continue
                raise
            try:
                resp = http.request(method, url, headers=headers, data=data, timeout=self.timeout)
            except NetworkError as exc:
                if not idempotent:
                    raise AmbiguousWrite(
                        f"{exc}; the write may or may not have been applied", hint=REPEAT_HINT
                    ) from None
                if attempt < self.max_attempts:
                    self._sleep(min(2**attempt, 30))
                    continue
                raise
            if resp.status == 200:
                payload = resp.json_or_none()
                if isinstance(payload, dict):
                    return payload
                if not idempotent:  # the write was accepted (HTTP 200); the read-back checks what happened
                    return {}
                if attempt < self.max_attempts:
                    self._sleep(min(2**attempt, 30))
                    continue
                raise ApiError(f"HTTP 200 for {method} {path}, but the body is not a JSON object", status=200)
            if resp.status == 401 and not refreshed:
                refreshed = True  # the access token expired early: refresh once and resend (401 = not applied)
                self._access_token = None
                attempt -= 1
                continue
            message, codes = describe_error(resp)
            if idempotent and resp.status in RETRY_STATUSES and attempt < self.max_attempts:
                self._sleep(self._retry_delay(resp, attempt))
                continue
            hint = google_hint(codes, resp.status)  # a one-line fix for errors we know (docs/troubleshooting.md)
            if resp.status == 429:
                raise RateLimited(message, status=429, code=codes, hint=hint or GOOGLE_QUOTA_HINT)
            if resp.status >= 500 and not idempotent:
                raise AmbiguousWrite(message, status=resp.status, code=codes, hint=REPEAT_HINT)
            raise ApiError(message, status=resp.status, code=codes, hint=hint)

    @staticmethod
    def _retry_delay(resp: http.Response, attempt: int) -> float:
        retry_after = resp.headers.get("retry-after", "")
        if retry_after.isdigit():
            return min(float(retry_after), 60.0)
        return float(min(2**attempt, 30))

    # ------------------------------------------------------------------ API
    def _cid(self, customer_id: str | None) -> str:
        cid = customer_id or self.customer_id
        if not cid:
            raise ConfigError("no Google Ads customer id", hint="set [google] customer_id or pass --customer-id")
        return cid

    def search(self, query: str, customer_id: str | None = None) -> list[dict[str, Any]]:
        """Run a GAQL query, following page tokens. Returns the result rows."""
        cid = self._cid(customer_id)
        rows: list[dict[str, Any]] = []
        page_token = None
        while True:
            body: dict[str, Any] = {"query": query.strip()}
            if page_token:
                body["pageToken"] = page_token
            res = self.call("POST", f"customers/{cid}/googleAds:search", body, idempotent=True)
            rows.extend(res.get("results") or [])
            page_token = res.get("nextPageToken")
            if not page_token:
                return rows

    def mutate(self, service: str, operations: list[dict[str, Any]], *, validate_only: bool) -> Any:
        body: dict[str, Any] = {"operations": operations}
        if validate_only:
            body["validateOnly"] = True
        return self.call("POST", f"customers/{self._cid(None)}/{service}:mutate", body, idempotent=validate_only)

    def list_accessible_customers(self) -> list[str]:
        res = self.call("GET", "customers:listAccessibleCustomers", idempotent=True, use_login=False)
        return [str(name).split("/")[-1] for name in res.get("resourceNames") or []]
