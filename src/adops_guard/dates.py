"""Date ranges in the ad account's own time zone.

Both platforms bucket spend by the account's time zone, and Google's
``LAST_N_DAYS`` ranges exclude today. Reports here therefore use explicit
ranges that end *today* in the account's time zone.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from adops_guard.errors import ConfigError

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - zoneinfo is in the stdlib from 3.9
    ZoneInfo = None  # type: ignore[assignment,misc]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def account_now(tz_name: str | None, now: datetime) -> tuple[datetime, bool]:
    """``now`` converted to the account time zone; the flag says whether the zone was known."""
    if tz_name and ZoneInfo is not None:
        try:
            return now.astimezone(ZoneInfo(tz_name)), True
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return now.astimezone(timezone.utc), False


def parse_date(text: str, what: str = "date") -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text or ""):
        raise ConfigError(f"{what} must look like YYYY-MM-DD (got {text!r})")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ConfigError(f"{what} is not a valid date: {text!r}") from None


def resolve_range(
    *, days: int | None, date_from: str | None, date_to: str | None, today: date, default_days: int = 7
) -> tuple[date, date]:
    if days is not None and (date_from or date_to):
        raise ConfigError("use --days or --from/--to, not both")
    if date_from or date_to:
        end = parse_date(date_to, "--to") if date_to else today
        start = parse_date(date_from, "--from") if date_from else end - timedelta(days=default_days - 1)
    else:
        count = default_days if days is None else days
        if not 1 <= count <= 366:
            raise ConfigError("--days must be between 1 and 366")
        end = today
        start = today - timedelta(days=count - 1)
    if start > end:
        raise ConfigError(f"the range starts after it ends ({start} > {end})")
    return start, end
