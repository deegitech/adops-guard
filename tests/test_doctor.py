from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adops_guard.cli import main
from fakes import (
    AD_ACCOUNT,
    APP_STORE_ID,
    CLIENT_ID,
    CLIENT_SECRET,
    CUSTOMER,
    IG_USER_ID,
    META_APP_ID,
    REFRESH_TOKEN,
    Harness,
    gaql_error,
)

POSIX = os.name != "nt"
ALL_PERMISSIONS = ("ads_read", "ads_management", "business_management", "pages_show_list", "pages_read_engagement")
GOOGLE_ENV = (
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
)


def ffprobe_ok(cmd, **kwargs):  # type: ignore[no-untyped-def]
    return SimpleNamespace(returncode=0, stdout="ffprobe version 7.1 Copyright (c) the FFmpeg developers\n", stderr="")


def closed_port_url() -> str:
    """A loopback URL nothing listens on: every request to it fails with a network error."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


TROUBLESHOOTING = "https://github.com/deegitech/adops-guard/blob/v"


class Terminal(io.StringIO):
    """A stdout that says it is a UTF-8 terminal."""

    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


class DoctorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.h.runner = ffprobe_ok
        g = self.h.google
        g.add_campaign("11111111111", "Brand")
        g.account_budgets = [{"id": "5", "status": "APPROVED", "approvedSpendingLimitMicros": "5000000000",
                              "amountServedMicros": "1180000000", "approvedStartDateTime": "2030-04-01 00:00:00"}]  # fmt: skip
        g.cost_micros = 1_465_000_000  # the real-time metrics are ahead of the billing counter
        g.conversions = [{"resourceName": f"customers/{CUSTOMER}/conversionActions/1", "id": "1", "type": "WEBPAGE",
                          "name": "Website: App Store button", "status": "ENABLED", "primaryForGoal": True}]  # fmt: skip
        m = self.h.graph
        m.permissions = [{"permission": name, "status": "granted"} for name in ALL_PERMISSIONS]
        m.account["spend_cap"] = "500000"
        m.usage_headers = {
            "X-Ad-Account-Usage": json.dumps({"acc_id_util_pct": 4, "ads_api_access_tier": "standard_access"})
        }

    def doctor(self, *argv: str, json_mode: bool = False):  # type: ignore[no-untyped-def]
        return self.h.run("doctor", *argv, json_mode=json_mode)

    def checks(self, *argv: str) -> dict[str, dict]:
        result = self.doctor(*argv, json_mode=True)
        checks = {check["name"]: check for check in result.json()["checks"]}
        for check in checks.values():  # every failed check comes with a fix
            if check["status"] == "fail":
                self.assertTrue(check["fix"], check)
        return checks

    def use_google_yaml(self, mode: int = 0o600) -> str:
        """Credentials from a temp google-ads.yaml instead of the environment (never the real ~/google-ads.yaml)."""
        for name in GOOGLE_ENV:
            del self.h.env[name]
        path = self.h.dir / "google-ads.yaml"
        path.write_text(f"client_id: {CLIENT_ID}\nclient_secret: {CLIENT_SECRET}\nrefresh_token: {REFRESH_TOKEN}\n")
        os.chmod(path, mode)
        self.h.config["google"]["credentials_file"] = str(path)
        return str(path)


class DoctorTests(DoctorTestCase):
    def test_a_healthy_setup_passes_and_changes_nothing(self) -> None:
        result = self.doctor()
        self.assertEqual(0, result.code, result.text)
        self.assertNotIn("FAIL", result.out)
        for text in (
            "OAuth: Google accepted the refresh token",
            'account 123-456-7890 "Example Co": ENABLED, USD, America/New_York',
            "1,465.00 USD of 5,000.00 USD used since 2030-04-01",  # the larger of the two counters
            "permissions: ads_read and ads_management are granted",
            f'Instagram identity: {IG_USER_ID} through Page "Example Page"',  # no instagram_basic: the id only
            "rate tier standard_access",
            "ffprobe 7.1",
        ):
            self.assertIn(text, result.out)
        self.assertRegex(result.out, r"Result: \d+ passed, 0 failed")
        self.assertEqual([], self.h.google.mutations, "doctor never sends a mutate, not even a validate-only one")
        self.assertEqual([], self.h.graph.posts, "doctor never POSTs to Meta")
        self.assertFalse(self.h.state_dir.exists(), "doctor does not create the state directory")

    def test_plain_ascii_in_logs_and_marks_in_a_terminal(self) -> None:
        result = self.doctor()
        self.assertTrue(result.out.isascii(), "logs and pipes get plain ASCII")
        self.assertIn("  ok     ", result.out)
        self.h.write_config()
        out = Terminal()
        code = main(
            ["--config", str(self.h.config_path), "doctor"],
            env=self.h.env, stdout=out, stderr=io.StringIO(), runner=ffprobe_ok, cwd=self.h.dir,
            sleep=lambda _: None, now=lambda: self.h.now, monotonic=lambda: 0.0,
        )  # fmt: skip
        self.assertEqual(0, code)
        self.assertIn("  ✓ OAuth: Google accepted the refresh token", out.getvalue())
        self.assertIn("  ! [google] api_base_url", out.getvalue())

    def test_json_is_one_document(self) -> None:
        result = self.doctor(json_mode=True)
        self.assertEqual(0, result.code, result.err)
        data = result.json()
        self.assertTrue(data["ok"])
        self.assertEqual(0, data["summary"]["fail"])
        names = {check["name"] for check in data["checks"]}
        self.assertTrue({"config", "google.oauth", "google.account_budget", "meta.permissions", "meta.rate"} <= names)
        self.assertIn("Result:", result.err)  # the human lines go to stderr

    def test_only_one_platform(self) -> None:
        checks = self.checks("google")
        self.assertIn("google.oauth", checks)
        self.assertFalse([name for name in checks if name.startswith("meta.")])

    def test_a_failure_names_where_the_docs_are(self) -> None:
        self.h.google.token_failures = [(400, {}, {"error": "invalid_grant", "error_description": "Bad Request"})]
        result = self.doctor("google")
        self.assertEqual(1, result.code)
        self.assertIn("Google steps 4 and 5", result.out)
        self.assertIn("Every error and its fix: " + TROUBLESHOOTING, result.out)
        self.assertIn("The docs/ files named above: https://github.com/deegitech/adops-guard/tree/v", result.out)

    def test_a_pasted_second_block_is_explained(self) -> None:
        self.h.write_config()
        self.h.config_path.write_text(self.h.config_path.read_text() + "[meta]\ntoken_source = keychain\n")
        out = io.StringIO()
        code = main(
            ["--config", str(self.h.config_path), "doctor"],
            env=self.h.env, stdout=out, stderr=io.StringIO(), runner=ffprobe_ok, cwd=self.h.dir,
            sleep=lambda _: None, now=lambda: self.h.now, monotonic=lambda: 0.0,
        )  # fmt: skip
        self.assertEqual(1, code)
        self.assertIn("section 'meta' already exists", out.getvalue())
        self.assertIn("each [section] and each key may appear only once", out.getvalue())
        self.assertNotIn("copy examples/adops-guard.example.ini", out.getvalue(), "the file exists: do not replace it")

    def test_a_broken_config_is_reported_and_the_platforms_wait(self) -> None:
        self.h.config["google"]["max_daily_budjet"] = "50"
        result = self.doctor()
        self.assertEqual(1, result.code)
        self.assertIn("unknown key [google] max_daily_budjet", result.out)
        self.assertIn("known keys:", result.out)
        self.assertIn("skipped until the config file loads", result.out)

    def test_an_unconfigured_platform_is_skipped_unless_named(self) -> None:
        del self.h.config["meta"]
        del self.h.env["META_ACCESS_TOKEN"]
        result = self.doctor()
        self.assertEqual(0, result.code, result.text)
        self.assertIn("Meta is not configured, so it was skipped", result.out)
        result = self.doctor("meta")
        self.assertEqual(1, result.code)
        self.assertIn("no Meta access token", result.out)
        self.assertIn("export META_ACCESS_TOKEN", result.out)

    def test_a_state_directory_others_can_change_fails(self) -> None:
        if not POSIX:
            self.skipTest("POSIX permissions")
        self.h.state_dir.mkdir()
        os.chmod(self.h.state_dir, 0o777)
        checks = self.checks()
        self.assertEqual("fail", checks["state"]["status"])
        self.assertEqual(f"chmod 700 {self.h.state_dir}", checks["state"]["fix"])

    def test_unfinished_writes_are_reported(self) -> None:
        self.h.state_dir.mkdir(mode=0o700)
        os.chmod(self.h.state_dir, 0o700)
        (self.h.state_dir / "state.json").write_text(
            json.dumps({"pending": {"meta:ad.status:120000000000000003": {"run": "abc", "expected": "ACTIVE"}}})
        )
        checks = self.checks()
        self.assertEqual("warn", checks["state.pending"]["status"])
        self.assertIn("meta:ad.status:120000000000000003", checks["state.pending"]["detail"])
        self.assertIn("re-run the same command", checks["state.pending"]["fix"])

    def test_missing_ffprobe_is_information_not_a_failure(self) -> None:
        def no_ffprobe(cmd, **kwargs):  # type: ignore[no-untyped-def]
            raise FileNotFoundError(cmd[0])

        self.h.runner = no_ffprobe
        result = self.doctor("meta", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        ffprobe = next(c for c in result.json()["checks"] if c["name"] == "meta.ffprobe")
        self.assertEqual("info", ffprobe["status"])
        self.assertIn("brew install ffmpeg", ffprobe["fix"])


class GoogleDoctorTests(DoctorTestCase):
    def test_a_credentials_file_others_can_read_fails_with_the_chmod(self) -> None:
        if not POSIX:
            self.skipTest("POSIX permissions")
        path = self.use_google_yaml(mode=0o644)
        result = self.doctor("google")
        self.assertEqual(1, result.code)
        self.assertIn("can be read by other users (mode 0o644)", result.out)
        self.assertIn(f"fix: run: chmod 600 {path}", result.out)
        self.assertEqual([], self.h.google.token_requests, "nothing is sent with credentials that failed the check")

    def test_a_private_credentials_file_passes(self) -> None:
        path = self.use_google_yaml()
        checks = self.checks("google")
        self.assertEqual("pass", checks["google.credentials"]["status"])
        self.assertIn(f"{path} (mode 0600, owned by you)", checks["google.credentials"]["detail"])
        self.assertNotIn("google.developer_token", checks)

    def test_a_refresh_token_from_a_testing_app_fails_with_the_fix(self) -> None:
        self.h.google.token_failures = [(400, {}, {"error": "invalid_grant", "error_description": "Bad Request"})]
        result = self.doctor("google")
        self.assertEqual(1, result.code)
        self.assertIn("invalid_grant", result.out)
        self.assertIn("expire after 7 days", result.out)
        self.assertIn("Google Auth Platform > Audience > Publish app", result.out)

    def test_test_access_on_a_production_account(self) -> None:
        self.h.google.on_query(
            r"customer\.descriptive_name",
            gaql_error(
                403, "authorizationError", "DEVELOPER_TOKEN_NOT_APPROVED", "The developer token is not approved."
            ),
        )
        checks = self.checks("google")
        self.assertEqual("fail", checks["google.account"]["status"])
        self.assertIn("Upgrade access level", checks["google.account"]["fix"])

    def test_a_cloud_project_with_test_access(self) -> None:
        # What API v25 answers when a Cloud project with Test access calls a production account (Google's note).
        self.h.google.on_query(
            r"customer\.descriptive_name",
            gaql_error(403, "authorizationError", "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION", "Not approved."),
        )
        check = self.checks("google")["google.account"]
        self.assertEqual("fail", check["status"])
        self.assertIn("still has Test access", check["fix"])
        self.assertIn("Upgrade access level > Apply for access", check["fix"])
        self.assertNotIn("check customer_id", check["fix"])

    def test_network_errors_are_not_blamed_on_the_setup(self) -> None:
        self.h.config["google"]["api_base_url"] = closed_port_url()  # the OAuth endpoint answers, the API does not
        check = self.checks("google")["google.access"]
        self.assertEqual("fail", check["status"])
        self.assertIn("network error", check["detail"])
        self.assertIn("not your setup", check["fix"])
        self.assertIn("HTTPS_PROXY", check["fix"])
        self.h.config["google"]["oauth_token_url"] = closed_port_url()
        check = self.checks("google")["google.oauth"]
        self.assertEqual("fail", check["status"])
        self.assertIn("not your setup", check["fix"])

    def test_a_5xx_is_a_service_problem(self) -> None:
        unavailable = {
            "error": {"code": 503, "message": "The service is currently unavailable.", "status": "UNAVAILABLE"}
        }
        self.h.google.list_failures = [(503, {}, unavailable)] * 3  # three attempts, then the doctor reports it
        check = self.checks("google")["google.access"]
        self.assertEqual("fail", check["status"])
        self.assertIn("not your setup", check["fix"])
        internal = {"error": {"code": 500, "message": "Internal error encountered.", "status": "INTERNAL"}}
        self.h.google.on_query(r"customer\.descriptive_name", (500, {}, internal))
        check = self.checks("google")["google.account"]
        self.assertEqual("fail", check["status"])
        self.assertIn("not your setup", check["fix"])
        self.assertNotIn("customer_id", check["fix"])

    def test_an_unknown_error_still_gets_a_fix(self) -> None:
        odd = {"error": {"code": 400, "message": "Something unusual.", "status": "INVALID_ARGUMENT"}}
        self.h.google.list_failures = [(400, {}, odd)]
        check = self.checks("google")["google.access"]
        self.assertEqual("fail", check["status"])
        self.assertIn("docs/troubleshooting.md (" + TROUBLESHOOTING, check["fix"])

    def test_a_manager_account_as_customer_id(self) -> None:
        self.h.google.customer["manager"] = True
        checks = self.checks("google")
        self.assertEqual("fail", checks["google.account"]["status"])
        self.assertIn("manager (MCC) account", checks["google.account"]["detail"])
        self.assertIn("login_customer_id", checks["google.account"]["fix"])

    def test_no_customer_id(self) -> None:
        del self.h.config["google"]["customer_id"]
        checks = self.checks()
        self.assertEqual("warn", checks["google.customer_id"]["status"])
        self.assertEqual("fail", self.checks("google")["google.customer_id"]["status"])

    def test_account_budget_headroom(self) -> None:
        self.h.google.cost_micros = 5_000_000_000  # metrics have reached the limit, the billing counter lags
        check = self.checks("google")["google.account_budget"]
        self.assertEqual("fail", check["status"])
        self.assertIn("account budget used up", check["detail"])
        self.assertIn("Billing > Account budget > Edit", check["fix"])
        self.h.google.cost_micros = 4_700_000_000
        check = self.checks("google")["google.account_budget"]
        self.assertEqual("warn", check["status"])
        self.assertIn("300.00 USD left", check["detail"])

    def test_no_account_budget_is_information(self) -> None:
        self.h.google.account_budgets = []
        check = self.checks("google")["google.account_budget"]
        self.assertEqual("info", check["status"])
        self.assertIn("spend-watch needs --since", check["detail"])

    def test_auto_apply(self) -> None:
        self.h.google.recommendation_subscriptions = [
            {"type": "KEYWORD", "status": "ENABLED"},
            {"type": "TARGET_CPA_OPT_IN", "status": "PAUSED"},
        ]
        result = self.doctor("google", json_mode=True)
        self.assertEqual(0, result.code, "a warning does not fail the doctor")
        check = next(c for c in result.json()["checks"] if c["name"] == "google.auto_apply")
        self.assertEqual("warn", check["status"])
        self.assertIn("1 recommendation type(s): KEYWORD", check["detail"])
        self.assertIn("Recommendations > Auto-apply", check["fix"])
        self.h.google.on_query(
            r"FROM recommendation_subscription", gaql_error(400, "queryError", "INVALID_RESOURCE", "x")
        )
        self.assertEqual("manual", self.checks("google")["google.auto_apply"]["status"])

    def test_only_secondary_conversion_actions(self) -> None:
        self.h.google.conversions[0]["primaryForGoal"] = False
        check = self.checks("google")["google.conversions"]
        self.assertEqual("warn", check["status"])
        self.assertIn("Misconfigured", check["detail"])
        self.h.google.conversions = []
        self.assertEqual("info", self.checks("google")["google.conversions"]["status"])


class MetaDoctorTests(DoctorTestCase):
    def test_missing_ads_management_fails_with_the_use_case(self) -> None:
        self.h.graph.permissions = [{"permission": "ads_read", "status": "granted"},
                                    {"permission": "ads_management", "status": "declined"}]  # fmt: skip
        result = self.doctor("meta")
        self.assertEqual(1, result.code)
        self.assertIn("permissions missing: ads_management (not granted: ads_management (declined))", result.out)
        self.assertIn("'Create & manage ads with Marketing API'", result.out)

    def test_a_token_cut_at_128_characters(self) -> None:
        token = "EAA" + "x" * 125  # what the macOS Keychain prompt leaves of a longer token
        self.h.env["META_ACCESS_TOKEN"] = token
        result = self.doctor("meta")
        self.assertEqual(1, result.code)
        self.assertIn("128 characters (never printed)", result.out)
        self.assertIn("the token is exactly 128 characters", result.out)
        self.assertIn('-w "$(pbpaste)"', result.out)
        self.assertNotIn(token, result.text)

    def test_meta_unreachable_or_failing_is_not_a_token_problem(self) -> None:
        self.h.config["meta"]["graph_base_url"] = closed_port_url()
        check = self.checks("meta")["meta.token_user"]
        self.assertEqual("fail", check["status"])
        self.assertIn("Meta did not answer", check["detail"])
        self.assertIn("not your setup", check["fix"])
        self.assertNotIn("generate a new token", check["fix"])
        self.h.config["meta"]["graph_base_url"] = self.h.server.url
        self.h.graph.get_failures = [(502, {}, "<html><body>502 Bad Gateway</body></html>")] * 3
        check = self.checks("meta")["meta.token_user"]
        self.assertIn("Meta did not answer", check["detail"])
        self.assertIn("not your setup", check["fix"])

    def test_a_5xx_on_the_ad_account_is_not_an_access_problem(self) -> None:
        original = self.h.graph.get

        def get(req, path):  # type: ignore[no-untyped-def]
            if path == AD_ACCOUNT:
                return (
                    503,
                    {},
                    {"error": {"message": "Service temporarily unavailable", "code": 2, "is_transient": True}},
                )
            return original(req, path)

        self.h.graph.get = get  # type: ignore[method-assign]
        check = self.checks("meta")["meta.ad_account"]
        self.assertEqual("fail", check["status"])
        self.assertIn("not your setup", check["fix"])
        self.assertNotIn("Assign people", check["fix"])

    def test_a_permission_error_on_the_ad_account(self) -> None:
        original = self.h.graph.get

        def get(req, path):  # type: ignore[no-untyped-def]
            if path == AD_ACCOUNT:
                return (
                    400,
                    {},
                    {"error": {"message": "(#200) Permissions error", "type": "OAuthException", "code": 200}},
                )
            return original(req, path)

        self.h.graph.get = get  # type: ignore[method-assign]
        check = self.checks("meta")["meta.ad_account"]
        self.assertIn("Assign people", check["fix"])
        self.assertNotIn("adops-guard doctor", check["fix"], "the doctor does not tell you to run the doctor")

    def test_other_errors_on_the_token_user(self) -> None:
        no_use_case = {"message": "(#3) Application does not have the capability to make this API call.",
                       "type": "OAuthException", "code": 3}  # fmt: skip
        self.h.graph.get_failures = [(400, {}, {"error": no_use_case})]
        check = self.checks("meta")["meta.token_user"]
        self.assertIn("reading the token's user failed", check["detail"])
        self.assertIn("Create & manage ads with Marketing API", check["fix"])
        self.h.graph.get_failures = [(400, {}, {"error": {"message": "(#100) Odd parameter", "code": 100}})]
        check = self.checks("meta")["meta.token_user"]
        self.assertIn("docs/troubleshooting.md", check["fix"])

    def test_a_token_that_is_not_a_facebook_login_token(self) -> None:
        self.h.env["META_ACCESS_TOKEN"] = "IG" + "x" * 150
        check = self.checks("meta")["meta.token_user"]
        self.assertEqual("fail", check["status"])
        self.assertIn("start with EAA", check["fix"])

    def test_a_stored_command_line_instead_of_a_token(self) -> None:
        self.h.env["META_ACCESS_TOKEN"] = 'security add-generic-password -s adops-guard-meta -w "$(pbpaste)"'
        check = self.checks("meta")["meta.token"]
        self.assertEqual("fail", check["status"])
        self.assertIn("contains whitespace", check["detail"])
        self.assertIn("two lines pasted at once", check["fix"])

    def test_an_app_that_cannot_go_live(self) -> None:
        del self.h.graph.app["privacy_policy_url"]
        check = self.checks("meta")["meta.app"]
        self.assertEqual("warn", check["status"])
        self.assertIn("has no privacy policy URL", check["detail"])
        self.assertIn("App settings > Basic", check["fix"])

    def test_unknown_app_fields_are_not_reported_as_missing(self) -> None:
        self.h.graph.app_fields.discard("icon_url")  # an API version without the field
        checks = self.checks("meta")
        self.assertNotIn("meta.app", checks)
        self.assertEqual("manual", checks["meta.app_mode"]["status"])
        self.assertIn("App Mode: Live", checks["meta.app_mode"]["fix"])

    def test_an_unsettled_ad_account(self) -> None:
        self.h.graph.account["account_status"] = 3
        check = self.checks("meta")["meta.ad_account"]
        self.assertEqual("fail", check["status"])
        self.assertIn("UNSETTLED", check["detail"])
        self.assertIn("Billing & payments", check["fix"])

    def test_an_ad_account_the_token_cannot_read(self) -> None:
        self.h.config["meta"]["ad_account_id"] = "act_999999999999999"
        check = self.checks("meta")["meta.ad_account"]
        self.assertEqual("fail", check["status"])
        self.assertIn("Assign people", check["fix"])

    def test_account_spending_limit(self) -> None:
        self.h.graph.account["spend_cap"] = "0"
        check = self.checks("meta")["meta.spending_limit"]
        self.assertEqual("warn", check["status"])
        self.assertIn("Account spending limit", check["fix"])
        self.h.graph.account["spend_cap"] = "12345"  # equal to amount_spent
        check = self.checks("meta")["meta.spending_limit"]
        self.assertEqual("fail", check["status"])
        self.assertIn("all ads stop", check["detail"])

    def test_prepaid_and_rate_tier(self) -> None:
        self.h.graph.account["is_prepay_account"] = True
        self.h.graph.usage_headers = {
            "X-Ad-Account-Usage": json.dumps({"acc_id_util_pct": 12, "ads_api_access_tier": "development_access"})
        }
        checks = self.checks("meta")
        self.assertIn("cannot be moved to another ad account", checks["meta.funding"]["detail"])
        self.assertEqual("info", checks["meta.rate"]["status"])
        self.assertIn("development_access", checks["meta.rate"]["detail"])
        self.assertIn("Ads Management Standard Access", checks["meta.rate"]["fix"])

    def test_a_rate_limit_stops_the_meta_checks(self) -> None:
        self.h.graph.usage_headers = {"X-App-Usage": json.dumps({"call_count": 96})}
        result = self.doctor("meta", json_mode=True)
        self.assertEqual(1, result.code)
        stopped = next(c for c in result.json()["checks"] if c["name"] == "meta.stopped")
        self.assertIn("usage is at 96%", stopped["detail"])

    def test_instagram_username_needs_instagram_basic(self) -> None:
        self.h.graph.permissions.append({"permission": "instagram_basic", "status": "granted"})
        check = self.checks("meta")["meta.instagram"]
        self.assertEqual("pass", check["status"])
        self.assertIn(f"@example_brand ({IG_USER_ID})", check["detail"])
        fields = [req.query.get("fields", [""])[0] for req in self.h.graph.gets if req.path.endswith("/me/accounts")]
        self.assertEqual(["id,name,instagram_business_account{id,username}"], fields)

    def test_instagram_identity(self) -> None:
        self.h.graph.pages[0].pop("instagram_business_account")
        check = self.checks("meta")["meta.instagram"]
        self.assertEqual("warn", check["status"])
        self.assertIn("Linked accounts > Instagram", check["fix"])
        self.h.graph.pages = []
        self.assertIn("tick the Page AND the Instagram account", self.checks("meta")["meta.instagram"]["fix"])
        self.h.graph.permissions = [p for p in self.h.graph.permissions if p["permission"] != "pages_show_list"]
        checks = self.checks("meta")
        self.assertEqual("manual", checks["meta.instagram"]["status"])
        self.assertEqual("info", checks["meta.permissions_optional"]["status"])

    def test_app_promotion(self) -> None:
        for wanted in (META_APP_ID, APP_STORE_ID, "id" + APP_STORE_ID):
            check = self.checks("meta", "--app", wanted)["meta.apps"]
            self.assertEqual("pass", check["status"], wanted)
        result = self.doctor("meta", "--app", "999999999999999")
        self.assertEqual(1, result.code)
        self.assertIn("cannot be advertised from act_123456789012345", result.out)
        self.assertIn("Add platform > iOS", result.out)
        self.assertIn("Add assets > Ad accounts", result.out)


class ExampleConfigDoctorTests(unittest.TestCase):
    def test_the_untouched_example_config_skips_both_platforms(self) -> None:
        """docs/setup.md, step 0: copy the example, run the doctor; nothing is set up yet, so nothing fails."""
        root = Path(__file__).resolve().parents[1]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home, work = Path(tmp.name) / "home", Path(tmp.name) / "adops"
        home.mkdir()
        work.mkdir()
        config = work / "adops-guard.ini"
        config.write_text((root / "examples" / "adops-guard.example.ini").read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(config, 0o600)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):  # never the real home
            code = main(
                ["doctor"], env={"HOME": str(home)}, stdout=out, stderr=io.StringIO(), runner=ffprobe_ok, cwd=work,
                sleep=lambda _: None, now=lambda: datetime(2030, 5, 3, 12, 0, tzinfo=timezone.utc), monotonic=lambda: 0.0,
            )  # fmt: skip
        self.assertEqual(0, code, out.getvalue())
        self.assertIn("Google Ads is not configured, so it was skipped", out.getvalue())
        self.assertIn("Meta is not configured, so it was skipped", out.getvalue())


if __name__ == "__main__":
    unittest.main()
