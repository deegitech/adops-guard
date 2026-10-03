from __future__ import annotations

import os
import unittest
from decimal import Decimal

from fakes import CUSTOMER, Harness, gaql_error

CAMPAIGN = "11111111111"
AD_GROUP = "22222222222"
ALL_ON = {"youtubeInStream": True, "youtubeInFeed": True, "youtubeShorts": True, "discover": True}


class GoogleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.g = self.h.google
        self.g.add_campaign(CAMPAIGN, "Brand", budget_micros=30_000_000)
        self.g.add_ad_group(AD_GROUP, CAMPAIGN, "Group A", channels=dict(ALL_ON))
        self.g.add_ad(AD_GROUP, "33333333333", "Video ad")

    def applied(self) -> list[dict]:
        return [m for m in self.g.mutations if not m["validate"]]

    def validated(self) -> list[dict]:
        return [m for m in self.g.mutations if m["validate"]]


class ReadCommandTests(GoogleTestCase):
    def test_status(self) -> None:
        self.g.metrics[CAMPAIGN] = {"impressions": "1000", "clicks": "50", "costMicros": "12500000"}
        self.g.account_budgets = [{"id": "5", "status": "APPROVED", "approvedSpendingLimitMicros": "500000000",
                                   "amountServedMicros": "120000000", "approvedStartDateTime": "2030-04-01 00:00:00"}]  # fmt: skip
        result = self.h.run("google", "status")
        self.assertEqual(0, result.code, result.err)
        self.assertIn('Account 123-456-7890 "Example Co"', result.out)
        self.assertIn("Brand", result.out)
        self.assertIn("30.00", result.out)
        self.assertIn("500.00", result.out)

    def test_status_json(self) -> None:
        result = self.h.run("google", "status", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        data = result.json()
        self.assertEqual(CUSTOMER, data["customer"]["id"])
        self.assertEqual(Decimal("30"), Decimal(data["campaigns"][0]["daily_budget"]))
        self.assertEqual("2030-05-03", data["period"]["to"])

    def test_report_by_day_uses_an_explicit_range_ending_today(self) -> None:
        self.g.on_query(r"segments\.date, metrics", [
            {"segments": {"date": "2030-05-02"}, "metrics": {"impressions": "100", "clicks": "5", "costMicros": "2500000"}},
            {"segments": {"date": "2030-05-03"}, "metrics": {"impressions": "200", "clicks": "10", "costMicros": "4000000"}},
        ])  # fmt: skip
        result = self.h.run("google", "report", "--days", "2", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        data = result.json()
        self.assertEqual(["2030-05-02", "2030-05-03"], [r["date"] for r in data["rows"]])
        self.assertEqual(15, data["total"]["clicks"])
        query = [q for q in self.g.queries if "segments.date, metrics" in q][0]
        self.assertIn("BETWEEN '2030-05-02' AND '2030-05-03'", query)
        self.assertIn("FROM customer", query)

    def test_report_by_campaign(self) -> None:
        self.g.metrics[CAMPAIGN] = {"impressions": "1000", "clicks": "40", "costMicros": "8000000"}
        result = self.h.run("google", "report", "--by", "campaign")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("Brand", result.out)
        self.assertIn("4.00%", result.out)

    def test_report_by_campaign_keeps_spend_of_removed_campaigns(self) -> None:
        self.g.metrics[CAMPAIGN] = {"impressions": "1000", "clicks": "40", "costMicros": "8000000"}
        self.g.add_campaign("11111111113", "Removed mid-period", status="REMOVED")
        self.g.metrics["11111111113"] = {"impressions": "500", "clicks": "10", "costMicros": "2000000"}
        self.g.add_campaign("11111111114", "Removed long ago", status="REMOVED")
        result = self.h.run("google", "report", "--by", "campaign", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        data = result.json()
        self.assertEqual(["11111111111", "11111111113"], [r["campaign_id"] for r in data["rows"]])
        self.assertEqual("10", data["total"]["cost"])

    def test_accounts(self) -> None:
        self.h.config["google"]["login_customer_id"] = "999-888-7777"
        result = self.h.run("google", "accounts", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        self.assertEqual([CUSTOMER], result.json()["accessible"])

    def test_missing_customer_id(self) -> None:
        del self.h.config["google"]["customer_id"]
        result = self.h.run("google", "status")
        self.assertEqual(2, result.code)
        self.assertIn("customer id", result.err)


class BudgetTests(GoogleTestCase):
    def test_dry_run_validates_and_writes_nothing(self) -> None:
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "25")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("daily budget 30.00 USD -> 25.00 USD", result.out)
        self.assertIn("DRY RUN", result.out)
        self.assertEqual(1, len(self.validated()))
        self.assertEqual([], self.applied())
        self.assertEqual(30_000_000, self.g.budgets[self.g.campaigns[CAMPAIGN]["budget"]]["amountMicros"])
        self.assertFalse(self.h.state_dir.exists(), "a dry run must not create the state directory")

    def test_apply_writes_reads_back_and_journals(self) -> None:
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "25.50", "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("VERIFY   read back 25.50 USD: OK", result.out)
        sent = self.applied()[0]["body"]["operations"][0]
        self.assertEqual(
            {"resourceName": self.g.campaigns[CAMPAIGN]["budget"], "amountMicros": "25500000"}, sent["update"]
        )
        self.assertEqual("amount_micros", sent["updateMask"])
        self.assertEqual(["intent", "applied", "verified"], [e["phase"] for e in self.h.journal()])
        self.assertEqual({}, self.h.state().get("pending"))
        if os.name != "nt":
            self.assertEqual(0o600, (self.h.state_dir / "journal.jsonl").stat().st_mode & 0o777)

    def test_same_value_is_a_no_op(self) -> None:
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "30", "--apply")
        self.assertEqual(0, result.code)
        self.assertIn("NO-OP", result.out)
        self.assertEqual([], self.g.mutations)

    def test_increase_above_the_ceiling_is_refused_before_any_request(self) -> None:
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "80", "--apply")
        self.assertEqual(3, result.code)
        self.assertIn("above the configured ceiling", result.err)
        self.assertEqual([], self.g.mutations)

    def test_increase_without_a_configured_ceiling_fails_closed(self) -> None:
        del self.h.config["google"]["max_daily_budget"]
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "31")
        self.assertEqual(3, result.code)
        self.assertIn("no ceiling is configured", result.err)

    def test_decrease_needs_no_ceiling(self) -> None:
        del self.h.config["google"]["max_daily_budget"]
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "10", "--apply")
        self.assertEqual(0, result.code, result.err)

    def test_override_limit(self) -> None:
        result = self.h.run(
            "google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "80", "--override-limit", "--apply"
        )
        self.assertEqual(0, result.code, result.err)
        self.assertIn("NOT applied", result.out)

    def test_shared_budget_needs_shared_ok(self) -> None:
        self.g.add_campaign("11111111113", "Shared", shared=True, refs=3)
        result = self.h.run("google", "budget", "set", "--campaign", "11111111113", "--amount", "20")
        self.assertEqual(3, result.code)
        self.assertIn("shared by 3 campaigns", result.err)
        result = self.h.run("google", "budget", "set", "--campaign", "11111111113", "--amount", "20", "--shared-ok")
        self.assertEqual(0, result.code, result.err)

    def test_read_back_mismatch_exits_4_and_is_journaled(self) -> None:
        self.g.ignore_updates = True
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "20", "--apply")
        self.assertEqual(4, result.code)
        self.assertIn("read back 30.00 USD, expected 20.00 USD", result.err)
        self.assertEqual("mismatch", self.h.journal()[-1]["phase"])

    def test_ambiguous_write_keeps_a_pending_marker_and_rerun_warns(self) -> None:
        self.g.mutate_failures = [gaql_error(500, "internalError", "INTERNAL_ERROR", "Internal error encountered.")]
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "20", "--apply")
        self.assertEqual(1, result.code)
        self.assertIn("re-run the same command", result.err)
        self.assertEqual(["intent", "ambiguous"], [e["phase"] for e in self.h.journal()])
        self.assertTrue(self.h.state()["pending"])
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "20", "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("did not finish", result.err)
        self.assertEqual({}, self.h.state()["pending"])

    def test_rerun_after_an_ambiguous_write_that_went_through_settles_it(self) -> None:
        self.g.mutate_failures = [gaql_error(500, "internalError", "INTERNAL_ERROR", "Internal error encountered.")]
        argv = ("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "20", "--apply")
        self.assertEqual(1, self.h.run(*argv).code)
        self.g.budgets[self.g.campaigns[CAMPAIGN]["budget"]]["amountMicros"] = 20_000_000  # it did go through
        result = self.h.run(*argv)
        self.assertEqual(0, result.code, result.err)
        self.assertIn("NO-OP", result.out)
        self.assertIn("is confirmed in place", result.out)
        self.assertEqual(["intent", "ambiguous", "verified"], [e["phase"] for e in self.h.journal()])
        self.assertEqual({}, self.h.state()["pending"])
        result = self.h.run(*argv)  # nothing left to warn about
        self.assertNotIn("did not finish", result.err)

    def test_rerun_after_an_ambiguous_write_that_did_not_go_through(self) -> None:
        self.g.mutate_failures = [gaql_error(500, "internalError", "INTERNAL_ERROR", "Internal error encountered.")]
        self.assertEqual(
            1, self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "20", "--apply").code
        )
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "30")  # the old value
        self.assertEqual(0, result.code, result.err)
        self.assertIn("is not in place", result.out)
        self.assertEqual("superseded", self.h.journal()[-1]["phase"])
        self.assertEqual({}, self.h.state()["pending"])

    def test_dry_run_canary_catches_a_validate_only_request_that_wrote(self) -> None:
        self.g.apply_validate_only = True  # a platform that ignores validateOnly
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "25")
        self.assertEqual(4, result.code)
        self.assertIn("changed during a validate-only request: 30.00 USD -> 25.00 USD", result.err)
        self.assertNotIn("DRY RUN", result.out)
        self.assertEqual(["dry-run-wrote"], [e["phase"] for e in self.h.journal()])

    def test_api_validation_error_stops_before_apply(self) -> None:
        self.g.validate_failures = [
            gaql_error(400, "campaignBudgetError", "NON_MULTIPLE_OF_MINIMUM_CURRENCY_UNIT", "bad")
        ]
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "20", "--apply")
        self.assertEqual(1, result.code)
        self.assertIn("NON_MULTIPLE_OF_MINIMUM_CURRENCY_UNIT", result.err)
        self.assertEqual([], self.applied())

    def test_unknown_campaign_and_bad_ids(self) -> None:
        self.assertEqual(3, self.h.run("google", "budget", "set", "--campaign", "99999999999", "--amount", "5").code)
        self.assertEqual(2, self.h.run("google", "budget", "set", "--campaign", "1 OR 1=1", "--amount", "5").code)
        self.assertEqual(2, self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "5.555").code)

    def test_lock_busy(self) -> None:
        if os.name == "nt":
            self.skipTest("flock")
        import fcntl

        self.h.state_dir.mkdir(mode=0o700)
        fd = os.open(self.h.state_dir / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = self.h.run("google", "budget", "set", "--campaign", CAMPAIGN, "--amount", "20", "--apply")
        self.assertEqual(3, result.code)
        self.assertIn("another adops-guard write is running", result.err)
        self.assertEqual([], self.applied())


class StatusTests(GoogleTestCase):
    def test_pause_campaign(self) -> None:
        result = self.h.run("google", "pause", "--campaign", CAMPAIGN, "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("PAUSED", self.g.campaigns[CAMPAIGN]["status"])
        self.assertEqual("campaign.pause", self.h.journal()[0]["action"])

    def test_enable_checks_the_campaign_budget_against_the_ceiling(self) -> None:
        self.g.campaigns[CAMPAIGN]["status"] = "PAUSED"
        self.g.budgets[self.g.campaigns[CAMPAIGN]["budget"]]["amountMicros"] = 90_000_000
        result = self.h.run("google", "enable", "--campaign", CAMPAIGN, "--apply")
        self.assertEqual(3, result.code)
        self.assertEqual("PAUSED", self.g.campaigns[CAMPAIGN]["status"])
        self.g.budgets[self.g.campaigns[CAMPAIGN]["budget"]]["amountMicros"] = 40_000_000
        result = self.h.run("google", "enable", "--campaign", CAMPAIGN, "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("ENABLED", self.g.campaigns[CAMPAIGN]["status"])

    def test_pause_ad_group_and_ad(self) -> None:
        self.assertEqual(0, self.h.run("google", "pause", "--ad-group", AD_GROUP, "--apply").code)
        self.assertEqual("PAUSED", self.g.ad_groups[AD_GROUP]["status"])
        result = self.h.run("google", "pause", "--ad", f"{AD_GROUP}~33333333333", "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("PAUSED", self.g.ads[f"{AD_GROUP}~33333333333"]["status"])

    def test_enable_ad_in_paused_campaign_warns(self) -> None:
        self.g.campaigns[CAMPAIGN]["status"] = "PAUSED"
        self.g.ads[f"{AD_GROUP}~33333333333"]["status"] = "PAUSED"
        result = self.h.run("google", "enable", "--ad", f"{AD_GROUP}~33333333333")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("the campaign is PAUSED", result.out)

    def test_pausing_a_removed_campaign_is_a_no_op(self) -> None:
        self.g.campaigns[CAMPAIGN]["status"] = "REMOVED"
        result = self.h.run("google", "pause", "--campaign", CAMPAIGN, "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("REMOVED and does not serve", result.out)
        self.assertEqual([], self.g.mutations)
        self.assertEqual(3, self.h.run("google", "enable", "--campaign", CAMPAIGN).code)

    def test_bad_ad_reference(self) -> None:
        self.assertEqual(2, self.h.run("google", "pause", "--ad", "123").code)


class ChannelControlTests(GoogleTestCase):
    def test_show_current_channels(self) -> None:
        result = self.h.run("google", "channel-controls", "--ad-group", AD_GROUP, json_mode=True)
        self.assertEqual(0, result.code, result.err)
        channels = result.json()["channels"]
        self.assertTrue(channels["youtube_in_stream"])
        self.assertFalse(channels["gmail"])

    def test_turn_off_in_stream_with_a_leaf_mask(self) -> None:
        result = self.h.run(
            "google", "channel-controls", "--ad-group", AD_GROUP, "--youtube-in-stream", "off", "--apply"
        )
        self.assertEqual(0, result.code, result.err)
        op = self.applied()[0]["body"]["operations"][0]
        self.assertEqual(
            "demand_gen_ad_group_settings.channel_controls.selected_channels.youtube_in_stream", op["updateMask"]
        )
        self.assertEqual(
            {"youtubeInStream": False}, op["update"]["demandGenAdGroupSettings"]["channelControls"]["selectedChannels"]
        )
        self.assertFalse(self.g.ad_groups[AD_GROUP]["channels"]["youtubeInStream"])
        self.assertTrue(self.g.ad_groups[AD_GROUP]["channels"]["youtubeShorts"])

    def test_unchanged_channels_are_a_no_op(self) -> None:
        result = self.h.run("google", "channel-controls", "--ad-group", AD_GROUP, "--youtube-shorts", "on", "--apply")
        self.assertEqual(0, result.code)
        self.assertIn("NO-OP", result.out)

    def test_refuses_non_demand_gen(self) -> None:
        self.g.campaigns[CAMPAIGN]["channel"] = "VIDEO"
        result = self.h.run("google", "channel-controls", "--ad-group", AD_GROUP, "--youtube-in-stream", "off")
        self.assertEqual(3, result.code)

    def test_refuses_turning_everything_off(self) -> None:
        self.g.ad_groups[AD_GROUP]["channels"] = {"youtubeShorts": True}
        result = self.h.run("google", "channel-controls", "--ad-group", AD_GROUP, "--youtube-shorts", "off")
        self.assertEqual(3, result.code)
        self.assertIn("at least one channel", result.err)

    def test_unknown_current_state_needs_all_six_options(self) -> None:
        self.g.ad_groups[AD_GROUP]["channels"] = None
        result = self.h.run("google", "channel-controls", "--ad-group", AD_GROUP, "--youtube-in-stream", "off")
        self.assertEqual(3, result.code)
        args = ["--youtube-in-stream", "off", "--youtube-in-feed", "on", "--youtube-shorts", "on",
                "--discover", "on", "--gmail", "off", "--display", "off"]  # fmt: skip
        result = self.h.run("google", "channel-controls", "--ad-group", AD_GROUP, *args, "--apply")
        self.assertEqual(0, result.code, result.err)


class ConversionActionTests(GoogleTestCase):
    def test_create_dry_run_then_apply_then_idempotent(self) -> None:
        name = "Website: App Store button"
        result = self.h.run("google", "conversion-action", "create", "--name", name)
        self.assertEqual(0, result.code, result.err)
        self.assertEqual([], self.g.conversions)
        create = self.validated()[0]["body"]["operations"][0]["create"]
        self.assertEqual("WEBPAGE", create["type"])
        self.assertEqual("OUTBOUND_CLICK", create["category"])
        self.assertFalse(create["primaryForGoal"])

        result = self.h.run("google", "conversion-action", "create", "--name", name, "--apply", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("AW-000000000/FAKE_LABEL_1", result.json()["send_to"])
        self.assertEqual(1, len(self.g.conversions))
        self.assertIn(f"{CUSTOMER}:{name}", self.h.state()["google_conversion_actions"])

        result = self.h.run("google", "conversion-action", "create", "--name", name, "--apply")
        self.assertEqual(0, result.code)
        self.assertIn("already exists", result.out)
        self.assertEqual(1, len(self.g.conversions))

    def test_name_with_quotes_is_escaped_in_gaql(self) -> None:
        result = self.h.run("google", "conversion-action", "create", "--name", "It's a click")
        self.assertEqual(0, result.code, result.err)
        self.assertTrue(any("'It\\'s a click'" in q for q in self.g.queries))

    def test_list(self) -> None:
        self.h.run("google", "conversion-action", "create", "--name", "One", "--apply")
        result = self.h.run("google", "conversion-action", "list", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        self.assertEqual(["One"], [a["name"] for a in result.json()["conversion_actions"]])


if __name__ == "__main__":
    unittest.main()
