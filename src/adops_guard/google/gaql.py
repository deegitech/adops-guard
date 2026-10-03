"""GAQL helpers: safe literals, ids and date ranges.

Values that end up inside a query are validated (ids must be digits) or
escaped (strings), so user input can never change the shape of a query.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from adops_guard.errors import ConfigError


def gaql_id(value: Any, what: str = "id") -> int:
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,20}", text):
        raise ConfigError(f"{what} must be digits only (got {value!r})")
    return int(text)


def gaql_string(value: str) -> str:
    if any(ch in value for ch in "\r\n"):
        raise ConfigError("line breaks are not allowed in GAQL strings")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def between(start: date, end: date) -> str:
    return f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"


def parse_ad_ref(value: str) -> tuple[int, int]:
    """``AD_GROUP_ID~AD_ID`` (the form used in Google Ads resource names)."""
    match = re.fullmatch(r"\s*(\d{1,20})~(\d{1,20})\s*", value or "")
    if not match:
        raise ConfigError(f"--ad must look like AD_GROUP_ID~AD_ID (got {value!r})")
    return int(match.group(1)), int(match.group(2))


def as_int(value: Any) -> int:
    """GAQL returns int64 values as strings and omits zeros entirely."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
