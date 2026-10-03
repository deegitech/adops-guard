"""Spend ceilings: the rule that makes accidental spending hard.

A change *increases spending* when it raises a budget or a cap, removes a
cap, or turns something on. Such a change is refused when:

* no ceiling is configured for it (so a missing or misspelled setting fails
  closed, not open), or
* the resulting amount is above the configured ceiling,

unless ``--override-limit`` is passed for that one command. Changes that
lower spending (pausing, lowering a budget or a cap) are never blocked here.
"""

from __future__ import annotations

from decimal import Decimal

from adops_guard.errors import GuardRefused
from adops_guard.money import fmt_amount


def check_ceiling(
    *,
    increasing: bool,
    amount: Decimal | None,
    ceiling: Decimal | None,
    setting: str,
    override: bool,
    currency: str | None,
    what: str,
) -> str | None:
    """Return a note to show with the plan, or raise :class:`GuardRefused`."""
    if not increasing:
        return None
    if override:
        return f"--override-limit: the {setting} ceiling was NOT applied to this change"
    if amount is None:
        raise GuardRefused(
            f"cannot check {what} against {setting}: the amount is unknown",
            hint="pass --override-limit if you have checked it yourself",
        )
    if ceiling is None:
        raise GuardRefused(
            f"refusing to increase spending: no ceiling is configured ({setting})",
            hint=f"set {setting} in the config file, or pass --override-limit for this one change",
        )
    if amount > ceiling:
        raise GuardRefused(
            f"{what} {fmt_amount(amount, currency)} is above the configured ceiling "
            f"{fmt_amount(ceiling, currency)} ({setting})",
            hint="lower the amount, raise the ceiling in the config file, or pass --override-limit",
        )
    return f"within the ceiling: {what} {fmt_amount(amount, currency)} <= {fmt_amount(ceiling, currency)} ({setting})"
