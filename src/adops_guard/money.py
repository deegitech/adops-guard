"""Exact money handling: Decimal amounts, Google micros and Meta minor units.

Floats never touch money here. Amounts come in as strings such as ``25`` or
``25.50`` and are converted exactly; anything ambiguous is refused.
"""

from __future__ import annotations

import re
from decimal import Decimal

from adops_guard.errors import ConfigError

MICROS_PER_UNIT = 1_000_000
_AMOUNT = re.compile(r"^(?:0|[1-9]\d{0,11})(?:\.\d{1,2})?$")

# Meta stores money in the currency's smallest unit (its "offset"). Most
# currencies use 100 (cents). This table is deliberately short: for any other
# currency, check Meta's currency table and set [meta] currency_offset.
META_OFFSET_100 = frozenset(
    {
        "AED", "AUD", "BRL", "CAD", "CHF", "CZK", "DKK", "EUR", "GBP", "HKD", "ILS", "INR",
        "MXN", "MYR", "NOK", "NZD", "PHP", "PLN", "SAR", "SEK", "SGD", "THB", "TRY", "USD", "ZAR",
    }
)  # fmt: skip
META_OFFSET_1 = frozenset({"JPY", "KRW"})


def parse_amount(text: str | int | Decimal, what: str = "amount") -> Decimal:
    """Parse a non-negative amount with at most two decimals (``25``, ``25.5``, ``25.50``)."""
    value = str(text).strip()
    if not _AMOUNT.match(value):
        raise ConfigError(f"{what} must be a plain number with at most two decimals, like 25 or 25.50 (got {text!r})")
    return Decimal(value)


def to_micros(amount: Decimal) -> int:
    micros = amount * MICROS_PER_UNIT
    if micros != micros.to_integral_value():
        raise ConfigError(f"{amount} has more precision than micros allow")
    return int(micros)


def from_micros(value: str | int | None) -> Decimal:
    return Decimal(int(value or 0)) / MICROS_PER_UNIT


def meta_offset(currency: str | None, override: int | None = None) -> int:
    """Return how many minor units make one unit of ``currency`` in Meta's API."""
    if override:
        return override
    code = (currency or "").upper()
    if code in META_OFFSET_100:
        return 100
    if code in META_OFFSET_1:
        return 1
    raise ConfigError(
        f"no built-in Meta currency offset for {code or 'an unknown currency'}",
        hint="check Meta's currency table and set [meta] currency_offset (usually 100) in the config",
    )


def to_minor(amount: Decimal, offset: int) -> int:
    value = amount * offset
    if value != value.to_integral_value():
        raise ConfigError(f"{amount} cannot be expressed in this currency's smallest unit")
    return int(value)


def from_minor(value: str | int | None, offset: int) -> Decimal:
    return Decimal(int(value or 0)) / offset


def fmt_amount(value: Decimal | None, currency: str | None = None) -> str:
    """``Decimal('1234.5')`` -> ``'1,234.50'`` (plus the currency code if given)."""
    if value is None:
        return "-"
    text = f"{value.quantize(Decimal('0.01')):,.2f}"
    return f"{text} {currency}" if currency else text
