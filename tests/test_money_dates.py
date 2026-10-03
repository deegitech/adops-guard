from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from adops_guard.dates import account_now, parse_date, resolve_range
from adops_guard.errors import ConfigError, GuardRefused
from adops_guard.guards import check_ceiling
from adops_guard.money import fmt_amount, from_micros, from_minor, meta_offset, parse_amount, to_micros, to_minor


class MoneyTests(unittest.TestCase):
    def test_parse_amount_accepts_plain_numbers(self) -> None:
        self.assertEqual(Decimal("25"), parse_amount("25"))
        self.assertEqual(Decimal("25.5"), parse_amount(" 25.5 "))
        self.assertEqual(Decimal("0.01"), parse_amount("0.01"))

    def test_parse_amount_refuses_anything_ambiguous(self) -> None:
        for bad in ("-1", "1e3", "25.505", "1,000", "", "abc", "01", "25.", ".5", "nan", "inf"):
            with self.assertRaises(ConfigError, msg=bad):
                parse_amount(bad)

    def test_micros_round_trip_is_exact(self) -> None:
        self.assertEqual(25_000_000, to_micros(Decimal("25")))
        self.assertEqual(12_340_000, to_micros(Decimal("12.34")))
        self.assertEqual(Decimal("12.34"), from_micros("12340000"))
        self.assertEqual(Decimal(0), from_micros(None))

    def test_meta_offsets(self) -> None:
        self.assertEqual(100, meta_offset("usd"))
        self.assertEqual(100, meta_offset("GBP"))
        self.assertEqual(1, meta_offset("JPY"))
        self.assertEqual(100, meta_offset("XYZ", override=100))
        with self.assertRaises(ConfigError):
            meta_offset("XYZ")

    def test_minor_units(self) -> None:
        self.assertEqual(2500, to_minor(Decimal("25.00"), 100))
        self.assertEqual(2500, to_minor(Decimal("2500"), 1))
        self.assertEqual(Decimal("25"), from_minor("2500", 100))
        with self.assertRaises(ConfigError):
            to_minor(Decimal("25.5"), 1)

    def test_format(self) -> None:
        self.assertEqual("1,234.50 USD", fmt_amount(Decimal("1234.5"), "USD"))
        self.assertEqual("-", fmt_amount(None))


class CeilingTests(unittest.TestCase):
    def kwargs(self, **over):  # type: ignore[no-untyped-def]
        base = dict(increasing=True, amount=Decimal("40"), ceiling=Decimal("50"), setting="[x] max", override=False,
                    currency="USD", what="budget")  # fmt: skip
        base.update(over)
        return base

    def test_decrease_is_never_blocked(self) -> None:
        self.assertIsNone(check_ceiling(**self.kwargs(increasing=False, ceiling=None, amount=Decimal("999"))))

    def test_increase_within_ceiling_passes_with_a_note(self) -> None:
        self.assertIn("within the ceiling", check_ceiling(**self.kwargs()) or "")

    def test_increase_above_ceiling_is_refused(self) -> None:
        with self.assertRaises(GuardRefused):
            check_ceiling(**self.kwargs(amount=Decimal("50.01")))

    def test_missing_ceiling_fails_closed(self) -> None:
        with self.assertRaises(GuardRefused):
            check_ceiling(**self.kwargs(ceiling=None))

    def test_unknown_amount_fails_closed(self) -> None:
        with self.assertRaises(GuardRefused):
            check_ceiling(**self.kwargs(amount=None))

    def test_override_skips_the_ceiling_but_says_so(self) -> None:
        note = check_ceiling(**self.kwargs(amount=Decimal("5000"), override=True)) or ""
        self.assertIn("NOT applied", note)


class DateTests(unittest.TestCase):
    def test_account_now_uses_the_account_time_zone(self) -> None:
        now = datetime(2030, 5, 3, 2, 30, tzinfo=timezone.utc)
        local, known = account_now("America/Los_Angeles", now)
        self.assertTrue(known)
        self.assertEqual(date(2030, 5, 2), local.date())
        _, known = account_now("Not/AZone", now)
        self.assertFalse(known)

    def test_resolve_range_includes_today(self) -> None:
        today = date(2030, 5, 3)
        self.assertEqual(
            (date(2030, 4, 27), today), resolve_range(days=None, date_from=None, date_to=None, today=today)
        )
        self.assertEqual((today, today), resolve_range(days=1, date_from=None, date_to=None, today=today))
        self.assertEqual(
            (date(2030, 3, 1), date(2030, 3, 31)),
            resolve_range(days=None, date_from="2030-03-01", date_to="2030-03-31", today=today),
        )

    def test_bad_ranges(self) -> None:
        today = date(2030, 5, 3)
        with self.assertRaises(ConfigError):
            resolve_range(days=0, date_from=None, date_to=None, today=today)
        with self.assertRaises(ConfigError):
            resolve_range(days=None, date_from="2030-05-05", date_to="2030-05-01", today=today)
        with self.assertRaises(ConfigError):
            parse_date("03.05.2030")
        with self.assertRaises(ConfigError):  # --days is not silently ignored next to --from/--to
            resolve_range(days=3, date_from="2030-05-01", date_to=None, today=today)


if __name__ == "__main__":
    unittest.main()
