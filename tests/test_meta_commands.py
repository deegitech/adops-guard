from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace

from fakes import Harness

CAMPAIGN = "120000000000000001"
ADSET = "120000000000000002"
AD = "120000000000000003"
VIDEO = "120000000000000009"


class MetaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.m = self.h.graph
        self.m.add("campaign", CAMPAIGN, objective="OUTCOME_APP_PROMOTION", name="App promo")
        self.m.add("adset", ADSET, campaign_id=CAMPAIGN, daily_budget="2500", name="US iOS")
        self.m.add("ad", AD, campaign_id=CAMPAIGN, adset_id=ADSET, name="Video 1")

    def real_posts(self) -> list[dict]:
        return [p for p in self.m.posts if not p["validate"]]


class ReadTests(MetaTestCase):
    def test_whoami(self) -> None:
        result = self.h.run("meta", "whoami", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        data = result.json()
        self.assertEqual(["ads_read", "ads_management"], data["granted"])
        self.assertEqual("Example Ads", data["account"]["name"])

    def test_status_with_campaign_detail(self) -> None:
        result = self.h.run("meta", "status", "--campaign", CAMPAIGN)
        self.assertEqual(0, result.code, result.err)
        self.assertIn("App promo", result.out)
        self.assertIn("25.00 USD/day", result.out)
        self.assertIn("Video 1", result.out)
        self.assertIn("spent 123.45 USD", result.out)

    def test_report(self) -> None:
        self.m.insights = [
            {"date_start": "2030-05-02", "date_stop": "2030-05-02", "campaign_id": CAMPAIGN, "campaign_name": "App promo",
             "spend": "12.50", "impressions": "1000", "reach": "800", "clicks": "25", "inline_link_clicks": "20"},
            {"date_start": "2030-05-03", "date_stop": "2030-05-03", "campaign_id": CAMPAIGN, "campaign_name": "App promo",
             "spend": "7.50", "impressions": "500", "reach": "450", "clicks": "5", "inline_link_clicks": "5"},
        ]  # fmt: skip
        result = self.h.run("meta", "report", "--by-day", "--days", "2", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        data = result.json()
        self.assertEqual("20.00", data["total"]["spend"])
        self.assertEqual(30, data["total"]["clicks"])
        req = next(r for r in self.m.gets if r.path.endswith("/insights"))
        self.assertEqual({"since": "2030-05-02", "until": "2030-05-03"}, json.loads(req.query["time_range"][0]))
        self.assertEqual(["1"], req.query["time_increment"])

    def test_usage(self) -> None:
        self.m.usage_headers = {"X-App-Usage": json.dumps({"call_count": 7, "total_time": 3, "total_cputime": 2})}
        result = self.h.run("meta", "usage", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        self.assertEqual(7, result.json()["percent"])

    def test_missing_account_and_token(self) -> None:
        del self.h.config["meta"]["ad_account_id"]
        self.assertEqual(2, self.h.run("meta", "status").code)
        self.h.config["meta"]["ad_account_id"] = "act_123456789012345"
        del self.h.env["META_ACCESS_TOKEN"]
        result = self.h.run("meta", "status")
        self.assertEqual(2, result.code)
        self.assertIn("no Meta access token", result.err)


class SetStatusTests(MetaTestCase):
    def test_pause_dry_run_and_apply(self) -> None:
        self.m.objects[AD]["status"] = "ACTIVE"
        result = self.h.run("meta", "set", "status", AD, "PAUSED")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("DRY RUN", result.out)
        self.assertEqual([], self.m.posts, "a Meta dry run sends no POST at all by default")
        self.assertIn("not sent to the API", result.out)
        result = self.h.run("meta", "set", "status", AD, "PAUSED", "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("PAUSED", self.m.objects[AD]["status"])
        self.assertEqual([True, False], [p["validate"] for p in self.m.posts], "--apply validates first")
        self.assertEqual(["intent", "applied", "verified"], [e["phase"] for e in self.h.journal()])

    def test_validate_flag_sends_a_validate_only_request(self) -> None:
        result = self.h.run("meta", "set", "status", AD, "ACTIVE", "--validate", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        self.assertTrue(result.json()["validated"])
        self.assertEqual([True], [p["validate"] for p in self.m.posts])
        self.assertEqual("PAUSED", self.m.objects[AD]["status"])

    def test_dry_run_canary_catches_meta_applying_a_validate_only_request(self) -> None:
        self.m.apply_validate_only = True  # what the canary is for: Meta ignoring execution_options
        result = self.h.run("meta", "set", "status", AD, "ACTIVE", "--validate")
        self.assertEqual(4, result.code)
        self.assertIn("changed during a validate-only request: PAUSED -> ACTIVE", result.err)
        self.assertEqual(["dry-run-wrote"], [e["phase"] for e in self.h.journal()])

    def test_rerun_after_an_ambiguous_write_that_went_through_settles_it(self) -> None:
        self.m.post_failures = [(503, {}, {"error": {"message": "Service temporarily unavailable", "code": 2}})]
        self.assertEqual(1, self.h.run("meta", "set", "status", AD, "ACTIVE", "--apply").code)
        self.assertTrue(self.h.state()["pending"])
        self.m.objects[AD]["status"] = "ACTIVE"  # it did go through
        result = self.h.run("meta", "set", "status", AD, "ACTIVE", "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("is confirmed in place", result.out)
        self.assertEqual({}, self.h.state()["pending"])
        self.assertEqual("verified", self.h.journal()[-1]["phase"])

    def test_activate_checks_the_effective_budget(self) -> None:
        self.m.objects[ADSET]["daily_budget"] = "9000"
        result = self.h.run("meta", "set", "status", AD, "ACTIVE", "--apply")
        self.assertEqual(3, result.code)
        self.assertIn("ad set", result.err)
        self.assertEqual([], self.m.posts)
        self.m.objects[ADSET]["daily_budget"] = "2500"
        result = self.h.run("meta", "set", "status", AD, "ACTIVE", "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("ACTIVE", self.m.objects[AD]["status"])

    def test_activating_an_abo_campaign_sums_its_active_ad_sets(self) -> None:
        self.m.objects[ADSET]["status"] = "ACTIVE"
        self.m.add("adset", "120000000000000004", campaign_id=CAMPAIGN, daily_budget="3500", status="ACTIVE")
        result = self.h.run("meta", "set", "status", CAMPAIGN, "ACTIVE")
        self.assertEqual(3, result.code)
        self.assertIn("60.00 USD", result.err)
        self.assertIn("sum of 2 active ad set(s)", result.err)

    def test_activating_a_campaign_without_active_ad_sets_needs_no_budget_check(self) -> None:
        del self.h.config["meta"]["max_daily_budget"]
        result = self.h.run("meta", "set", "status", CAMPAIGN, "ACTIVE")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("nothing spends until one is", result.out)

    def test_lifetime_budget_uses_its_own_ceiling(self) -> None:
        del self.m.objects[ADSET]["daily_budget"]
        self.m.objects[ADSET]["lifetime_budget"] = "60000"
        self.assertEqual(3, self.h.run("meta", "set", "status", ADSET, "ACTIVE").code)
        self.h.config["meta"]["max_lifetime_budget"] = "700"
        self.assertEqual(0, self.h.run("meta", "set", "status", ADSET, "ACTIVE").code)

    def test_objects_from_other_accounts_are_refused(self) -> None:
        self.m.objects[AD]["account_id"] = "999999999999999"
        result = self.h.run("meta", "set", "status", AD, "PAUSED", "--apply")
        self.assertEqual(3, result.code)
        self.assertIn("not the configured act_123456789012345", result.err)
        self.assertEqual([], self.m.posts)

    def test_type_assertion_and_no_op(self) -> None:
        self.assertEqual(3, self.h.run("meta", "set", "status", AD, "PAUSED", "--type", "adset").code)
        result = self.h.run("meta", "set", "status", AD, "PAUSED")
        self.assertEqual(0, result.code)
        self.assertIn("NO-OP", result.out)

    def test_read_back_mismatch(self) -> None:
        self.m.ignore_posts = True
        self.assertEqual(4, self.h.run("meta", "set", "status", AD, "ACTIVE", "--apply").code)


class BudgetTests(MetaTestCase):
    def test_daily_budget_in_minor_units(self) -> None:
        result = self.h.run("meta", "set", "budget", ADSET, "--daily", "20", "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("2000", self.m.objects[ADSET]["daily_budget"])
        self.assertEqual({"daily_budget": "2000"}, {k: v for k, v in self.real_posts()[0]["form"].items()})
        self.assertIn("25.00 USD -> 20.00 USD", result.out)

    def test_increase_above_ceiling_and_below_meta_minimum(self) -> None:
        self.assertEqual(3, self.h.run("meta", "set", "budget", ADSET, "--daily", "60").code)
        self.assertEqual(3, self.h.run("meta", "set", "budget", ADSET, "--daily", "0.50").code)
        self.assertEqual([], self.m.posts)

    def test_wrong_budget_kind_and_ads(self) -> None:
        self.assertEqual(3, self.h.run("meta", "set", "budget", ADSET, "--lifetime", "100").code)
        self.assertEqual(3, self.h.run("meta", "set", "budget", AD, "--daily", "10").code)

    def test_currency_offset_1(self) -> None:
        self.m.account["currency"] = "JPY"
        self.m.account["min_daily_budget"] = "100"
        self.h.config["meta"]["max_daily_budget"] = "5000"
        result = self.h.run("meta", "set", "budget", ADSET, "--daily", "2000", "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("2000", self.m.objects[ADSET]["daily_budget"])

    def test_unknown_currency_needs_an_offset(self) -> None:
        self.m.account["currency"] = "XYZ"
        result = self.h.run("meta", "set", "budget", ADSET, "--daily", "20")
        self.assertEqual(2, result.code)
        self.assertIn("currency_offset", result.err)


class SpendCapTests(MetaTestCase):
    def test_add_a_cap_where_there_was_none_is_always_allowed(self) -> None:
        del self.h.config["meta"]["max_spend_cap"]
        result = self.h.run("meta", "spend-cap", CAMPAIGN, "--amount", "250", "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("25000", self.m.objects[CAMPAIGN]["spend_cap"])

    def test_raise_is_checked_and_lower_is_not(self) -> None:
        self.m.objects[CAMPAIGN]["spend_cap"] = "25000"
        self.assertEqual(3, self.h.run("meta", "spend-cap", CAMPAIGN, "--amount", "1500").code)
        self.assertEqual(0, self.h.run("meta", "spend-cap", CAMPAIGN, "--amount", "800", "--apply").code)
        self.assertEqual("80000", self.m.objects[CAMPAIGN]["spend_cap"])
        self.assertEqual(0, self.h.run("meta", "spend-cap", CAMPAIGN, "--amount", "200", "--apply").code)

    def test_below_meta_minimum(self) -> None:
        self.assertEqual(3, self.h.run("meta", "spend-cap", CAMPAIGN, "--amount", "50").code)

    def test_remove_needs_override(self) -> None:
        self.m.objects[CAMPAIGN]["spend_cap"] = "25000"
        self.assertEqual(3, self.h.run("meta", "spend-cap", CAMPAIGN, "--remove", "--apply").code)
        result = self.h.run("meta", "spend-cap", CAMPAIGN, "--remove", "--override-limit", "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("922337203685478", self.m.objects[CAMPAIGN]["spend_cap"])
        self.assertIn("read back none: OK", result.out)

    def test_only_campaigns(self) -> None:
        self.assertEqual(3, self.h.run("meta", "spend-cap", ADSET, "--amount", "250").code)


class VideoCheckTests(MetaTestCase):
    def test_remote_eligibility(self) -> None:
        self.m.add(
            "video", VIDEO, title="Cut A", length=14.8, status={"video_status": "ready"}, is_instagram_eligible=True
        )
        self.m.add(
            "video",
            "120000000000000010",
            title="Cut B",
            length=18.0,
            status={"video_status": "ready"},
            is_instagram_eligible=False,
        )
        result = self.h.run("meta", "video-check", VIDEO, json_mode=True)
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("eligible", result.json()["results"][0]["verdict"])
        result = self.h.run("meta", "video-check", VIDEO, "120000000000000010")
        self.assertEqual(3, result.code)
        self.assertIn("NOT eligible", result.out)
        self.assertIn("over the observed limit", result.out)
        self.assertIn("observed (Oct 2026)", result.out)

    def test_local_files_with_ffprobe(self) -> None:
        clip = self.h.dir / "clip.mp4"
        clip.write_bytes(b"not really a video")
        durations = iter(["18.2\n", "14.80\n"])
        calls = []

        def runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout=next(durations), stderr="")

        self.h.runner = runner
        del self.h.env["META_ACCESS_TOKEN"]  # local checks need no token
        result = self.h.run("meta", "video-check", "--file", str(clip))
        self.assertEqual(3, result.code)
        self.assertIn("LIKELY NOT eligible", result.out)
        result = self.h.run("meta", "video-check", "--file", str(clip))
        self.assertEqual(0, result.code)
        self.assertEqual("ffprobe", calls[0][0])
        self.assertEqual("file:" + os.path.abspath(clip), calls[0][-1])

    def test_a_file_name_starting_with_a_dash_is_not_an_ffprobe_option(self) -> None:
        (self.h.dir / "-report.mp4").write_bytes(b"x")
        calls = []

        def runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="10.0\n", stderr="")

        self.h.runner = runner
        cwd = os.getcwd()
        os.chdir(self.h.dir)
        self.addCleanup(os.chdir, cwd)
        result = self.h.run("meta", "video-check", "--file=-report.mp4")
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("file:" + os.path.join(os.getcwd(), "-report.mp4"), calls[0][-1])
        self.assertFalse(any(arg == "-report.mp4" for arg in calls[0]))

    def test_missing_ffprobe_is_unknown_not_a_failure(self) -> None:
        clip = self.h.dir / "clip.mp4"
        clip.write_bytes(b"x")

        def runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
            raise FileNotFoundError("ffprobe")

        self.h.runner = runner
        result = self.h.run("meta", "video-check", "--file", str(clip))
        self.assertEqual(0, result.code)
        self.assertIn("ffprobe not found", result.out)

    def test_needs_something_to_check(self) -> None:
        self.assertEqual(2, self.h.run("meta", "video-check").code)


if __name__ == "__main__":
    unittest.main()
