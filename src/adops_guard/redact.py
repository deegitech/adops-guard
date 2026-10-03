"""Keep secrets out of output, errors, journals and state files.

Two layers:

* :class:`Secret` wraps a credential so it cannot be printed, formatted,
  pickled or JSON-encoded by accident. Code that really needs the value calls
  :meth:`Secret.reveal` at the last moment, to build a request header or the
  OAuth request body.
* :func:`redact` scrubs any text: every value that was ever wrapped in a
  ``Secret`` is replaced, and so are well-known token shapes (Meta ``EAA...``,
  Google ``ya29.`` access tokens, ``1//`` refresh tokens, ``GOCSPX-`` client
  secrets, ``Bearer ...`` and ``key=value`` pairs whose key looks secret).
"""

from __future__ import annotations

import hmac
import re
import threading
from typing import Any

MASK = "***"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+(?!\*\*\*)[A-Za-z0-9._~+/=-]{6,}"), r"\1 ***"),
    (
        re.compile(
            r"(?i)\b((?:access|refresh|input|id)_token|client_secret|developer[-_]token|authorization|password|api_key)"
            r"(['\"]?\s*[:=]\s*['\"]?)(?!\*\*\*)(?!bearer\b)([^\s\"'&,;}]+)"
        ),
        r"\1\2***",
    ),
    (re.compile(r"\bEAA[A-Za-z0-9]{16,}"), "EAA***"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]{10,}"), "ya29.***"),
    (re.compile(r"(?<![A-Za-z0-9])1//[A-Za-z0-9._-]{10,}"), "1//***"),
    (re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{8,}"), "GOCSPX-***"),
)

SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "client_secret",
        "developer_token",
        "developer-token",
        "authorization",
        "password",
        "token",
        "input_token",
        "api_key",
    }
)


class Redactor:
    """Replaces registered secret values and token-shaped strings in text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()

    def register(self, value: str | None) -> None:
        # Very short values would turn ordinary words into "***"; real tokens are long.
        if value and len(value) >= 8:
            with self._lock:
                self._values.add(value)

    def forget_all(self) -> None:
        with self._lock:
            self._values.clear()

    def __call__(self, text: Any) -> str:
        out = text if isinstance(text, str) else str(text)
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            if value in out:
                out = out.replace(value, MASK)
        for pattern, replacement in _PATTERNS:
            out = pattern.sub(replacement, out)
        return out


REDACTOR = Redactor()


def redact(text: Any) -> str:
    """Return ``text`` as a string with every known secret masked."""
    return REDACTOR(text)


def redact_data(value: Any) -> Any:
    """Deep-copy JSON-like data, masking sensitive keys and secret values (in keys too)."""
    if isinstance(value, Secret):
        return MASK
    if isinstance(value, dict):
        return {
            (redact(key) if isinstance(key, str) else key): (
                MASK if str(key).lower() in SENSITIVE_KEYS else redact_data(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


class Secret:
    """A credential that refuses to be shown.

    ``str()``, ``repr()``, ``format()`` and f-strings all give ``***``;
    ``json.dumps`` and ``pickle`` raise. Creating a ``Secret`` also registers
    the value with the global redactor, so it is masked in any text later.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("Secret expects a string")
        self._value = value
        REDACTOR.register(value)

    def reveal(self) -> str:
        """Return the raw value. Use only to build a request, never to display."""
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    def __str__(self) -> str:
        return MASK

    def __format__(self, spec: str) -> str:
        return MASK

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and hmac.compare_digest(self._value, other._value)

    __hash__ = None  # type: ignore[assignment]

    def __reduce__(self) -> Any:
        raise TypeError("Secret values cannot be pickled")
