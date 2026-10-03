from __future__ import annotations

import re
import socket
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adops_guard.config import MetaSettings
from adops_guard.credentials import GoogleCredentials, load_meta_token
from adops_guard.errors import ApiError, CredentialError, NetworkError, RateLimited
from adops_guard.google.client import GoogleAdsClient
from adops_guard.hints import (
    GOOGLE_HINTS,
    GOOGLE_NEW_REFRESH_TOKEN,
    GOOGLE_QUOTA_HINT,
    GOOGLE_VERSION_HINT,
    META_CODE_HINTS,
    META_PAIR_HINTS,
    META_PERMISSION_HINT,
    META_RATE_HINT,
    META_SUBCODE_HINTS,
    NETWORK_HINT,
    OAUTH_DEFAULT_HINT,
    OAUTH_HINTS,
    google_hint,
    meta_hint,
    oauth_hint,
)
from adops_guard.meta.client import MetaClient
from adops_guard.redact import REDACTOR, Secret
from fakes import (
    AD_ACCOUNT,
    CLIENT_ID,
    CLIENT_SECRET,
    META_TOKEN,
    REFRESH_TOKEN,
    FakeGoogleAds,
    FakeGraph,
    Harness,
    gaql_error,
)
from mock_http import MockServer

ROOT = Path(__file__).resolve().parents[1]
ALL_HINTS = [
    *META_PAIR_HINTS.values(), *META_SUBCODE_HINTS.values(), *META_CODE_HINTS.values(), META_RATE_HINT,
    META_PERMISSION_HINT, *GOOGLE_HINTS.values(), GOOGLE_QUOTA_HINT, GOOGLE_VERSION_HINT, *OAUTH_HINTS.values(),
    OAUTH_DEFAULT_HINT, NETWORK_HINT,
]  # fmt: skip


def credentials() -> GoogleCredentials:
    return GoogleCredentials(None, Secret(CLIENT_ID), Secret(CLIENT_SECRET), Secret(REFRESH_TOKEN), None, "test")


class MetaMappingTests(unittest.TestCase):
    def test_observed_subcodes(self) -> None:
        for (code, sub), words in {
            (100, 1885183): "Development mode",
            (31, 3858385): "security check",
            (100, 1487810): "OUTCOME_APP_PROMOTION",
            (100, 2446880): "Linked accounts > WhatsApp",
            (100, 3858504): "creative_features_spec",
            (100, 4834011): "is_adset_budget_sharing_enabled",
            (100, 2490589): "explore_home",
        }.items():
            self.assertIn(words, meta_hint(code, sub) or "", (code, sub))
        self.assertEqual(meta_hint(100, 1885183), meta_hint(368, 1885183), "these subcodes decide whatever the code")

    def test_token_errors(self) -> None:
        self.assertIn("expired", meta_hint(190, 463) or "")
        self.assertIn('"$(pbpaste)"', meta_hint(190, 467) or "")
        self.assertIn("password change", meta_hint(190, 460) or "")
        self.assertIn("checkpoint", meta_hint(190, 459) or "")
        self.assertIn("Extend Access Token", meta_hint(190) or "")
        self.assertEqual(META_CODE_HINTS[190], meta_hint(190, 999), "an unknown subcode falls back to the code")
        self.assertEqual(meta_hint(190, 463), meta_hint("190", "463"), "numbers may arrive as strings")

    def test_codes_and_ranges(self) -> None:
        for code in (4, 17, 32, 613, 80000, 80004, 80014):
            self.assertEqual(META_RATE_HINT, meta_hint(code), code)
        for code in (200, 294, 299):
            self.assertEqual(META_PERMISSION_HINT, meta_hint(code), code)
        self.assertIn("Create & manage ads with Marketing API", meta_hint(10) or "")
        self.assertIn("Create & manage ads with Marketing API", meta_hint(3) or "", "no Marketing API capability")

    def test_development_access_tier(self) -> None:
        hint = meta_hint(270) or ""  # a code in the 200-299 range with its own, sharper fix
        self.assertNotEqual(META_PERMISSION_HINT, hint)
        self.assertIn("admin of both the app and the ad account", hint)
        self.assertIn("Marketing API Access Tier", hint)
        self.assertIn("Ads Management Standard Access", hint)
        self.assertEqual(hint, meta_hint("270", None))
        self.assertIn("Assign people", meta_hint(100, 33) or "")
        self.assertIn("api_version", meta_hint(2635) or "")
        self.assertIn("accountquality", meta_hint(368) or "")

    def test_unknown_errors_get_no_guess(self) -> None:
        self.assertIsNone(meta_hint(100))  # "invalid parameter" alone is too generic to guess a fix
        self.assertIsNone(meta_hint(None))
        self.assertIsNone(meta_hint("x", "y"))
        self.assertIsNone(meta_hint(80015))


class GoogleMappingTests(unittest.TestCase):
    def test_codes(self) -> None:
        for code, words in {
            "USER_PERMISSION_DENIED": "login_customer_id",
            "DEVELOPER_TOKEN_NOT_APPROVED": "Upgrade access level",
            "RESOURCE_EXHAUSTED": "2,880",
            "SERVICE_DISABLED": "Library > Google Ads API > Enable",
            "ACCESS_TOKEN_SCOPE_INSUFFICIENT": "auth/adwords",
            "ONE_WEBSITE_PER_AD_GROUP": "ad group of their own",
            "TWO_STEP_VERIFICATION_NOT_ENROLLED": "2-Step Verification",
            "NOT_ADS_USER": "ads.google.com",
            "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION": "Upgrade access level > Apply for access",
        }.items():
            self.assertIn(words, google_hint([code]) or "", code)

    def test_test_access_on_older_api_versions(self) -> None:
        # v25 answers CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION; older versions ACTION_NOT_PERMITTED (Google's note).
        self.assertIn("Test access", google_hint(["CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"]) or "")
        hint = google_hint(["ACTION_NOT_PERMITTED"]) or ""
        self.assertIn("Standard or Admin", hint)
        self.assertIn("before v25", hint)
        self.assertIn("Upgrade access level", hint)

    def test_a_new_refresh_token_goes_into_the_yaml(self) -> None:
        # Step 4 only creates it (gcloud's file); step 5 copies it into google-ads.yaml.
        self.assertIn("Google steps 4 and 5", GOOGLE_NEW_REFRESH_TOKEN)
        for hint in (oauth_hint("invalid_grant"), oauth_hint("unauthorized_client"), google_hint(["NOT_ADS_USER"])):
            self.assertIn("Google steps 4 and 5", hint or "")

    def test_first_known_code_wins_then_the_status(self) -> None:
        both = google_hint(["INTERNAL_ERROR", "CUSTOMER_NOT_ENABLED", "NOT_ADS_USER"])
        self.assertEqual(GOOGLE_HINTS["CUSTOMER_NOT_ENABLED"], both)
        self.assertEqual(GOOGLE_QUOTA_HINT, google_hint([], 429))
        self.assertEqual(GOOGLE_VERSION_HINT, google_hint([], 404))
        self.assertIsNone(google_hint(["INTERNAL_ERROR"], 500))

    def test_oauth(self) -> None:
        self.assertIn("7 days", oauth_hint("invalid_grant"))
        self.assertIn("Publish app", oauth_hint("invalid_grant"))
        self.assertIn("same Desktop-app client", oauth_hint("invalid_client"))
        self.assertIn("different OAuth client", oauth_hint("unauthorized_client"))
        self.assertEqual(OAUTH_DEFAULT_HINT, oauth_hint("access_denied"))
        self.assertEqual(OAUTH_DEFAULT_HINT, oauth_hint(None))


class HintTextTests(unittest.TestCase):
    def test_every_hint_is_one_plain_ascii_line(self) -> None:
        for hint in ALL_HINTS:
            self.assertTrue(hint.isascii(), hint)
            self.assertNotIn("\n", hint)
            self.assertNotIn("->", hint, "menu steps use '>'")

    def test_the_troubleshooting_guide_lists_every_known_error(self) -> None:
        text = (ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
        for code, sub in META_PAIR_HINTS:
            self.assertIn(f"`{code}/{sub}`", text)
        for sub in META_SUBCODE_HINTS:
            self.assertRegex(text, rf"`\d+/{sub}`")
        for code in META_CODE_HINTS:
            self.assertIn(f"`{code}`", text)
        for name in [*GOOGLE_HINTS, *OAUTH_HINTS]:
            self.assertIn(f"`{name}`", text)

    def test_the_docs_the_hints_point_at_exist(self) -> None:
        for hint in ALL_HINTS:
            for doc in re.findall(r"docs/[a-z-]+\.md", hint):
                self.assertTrue((ROOT / doc).is_file(), doc)


class ClientHintTests(unittest.TestCase):
    """The clients attach the hint to the error they raise."""

    def setUp(self) -> None:
        self.addCleanup(REDACTOR.forget_all)

    def test_meta_client(self) -> None:
        graph = FakeGraph()
        server = MockServer(graph).start()
        self.addCleanup(server.stop)
        client = MetaClient(Secret(META_TOKEN), base_url=server.url, sleep=lambda _: None, allow_test_endpoints=True)
        expired = {"message": "Error validating access token: Session has expired", "type": "OAuthException",
                   "code": 190, "error_subcode": 463}  # fmt: skip
        graph.get_failures = [(400, {}, {"error": expired})]
        with self.assertRaises(ApiError) as ctx:
            client.get(AD_ACCOUNT, {"fields": "id"})
        self.assertIn("token expired", ctx.exception.hint or "")
        graph.get_failures = [(400, {}, {"error": {"message": "User request limit reached", "code": 17}})]
        with self.assertRaises(RateLimited) as ctx:
            client.get(AD_ACCOUNT, {"fields": "id"})
        self.assertEqual(META_RATE_HINT, ctx.exception.hint)

    def test_google_client(self) -> None:
        fake = FakeGoogleAds()
        server = MockServer(fake).start()
        self.addCleanup(server.stop)

        def client(**kwargs):  # type: ignore[no-untyped-def]
            return GoogleAdsClient(credentials(), "123-456-7890", api_base_url=server.url,
                                   oauth_token_url=server.url + "/token", sleep=lambda _: None,
                                   allow_test_endpoints=True, **kwargs)  # fmt: skip

        disabled = {"error": {"code": 403, "status": "PERMISSION_DENIED",
                              "message": "Google Ads API has not been used in project 123 before or it is disabled.",
                              "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                           "reason": "SERVICE_DISABLED", "domain": "googleapis.com"}]}}  # fmt: skip
        fake.search_failures = [(403, {}, disabled)]
        with self.assertRaises(ApiError) as ctx:
            client().search("SELECT campaign.id FROM campaign")
        self.assertEqual(["SERVICE_DISABLED"], ctx.exception.code)
        self.assertIn("Google Ads API > Enable", ctx.exception.hint or "")

        fake.search_failures = [gaql_error(429, "quotaError", "RESOURCE_EXHAUSTED", "Too many requests")] * 3
        with self.assertRaises(RateLimited) as ctx:
            client().search("SELECT campaign.id FROM campaign")
        self.assertIn("Basic access", ctx.exception.hint or "")

        with self.assertRaises(ApiError) as ctx:  # the fake serves v25 only, like a retired version would 404
            client(api_version="v24").search("SELECT campaign.id FROM campaign")
        self.assertEqual(GOOGLE_VERSION_HINT, ctx.exception.hint)

        fake.token_failures = [(401, {}, {"error": "invalid_client", "error_description": "Unauthorized"})]
        with self.assertRaises(CredentialError) as ctx:
            client().search("SELECT campaign.id FROM campaign")
        self.assertIn("same Desktop-app client", ctx.exception.hint or "")

    def test_network_errors_get_the_network_hint(self) -> None:
        with socket.socket() as sock:  # a loopback port nothing listens on
            sock.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        client = MetaClient(Secret(META_TOKEN), base_url=url, sleep=lambda _: None, allow_test_endpoints=True)
        with self.assertRaises(NetworkError) as ctx:
            client.get("me")
        self.assertEqual(NETWORK_HINT, ctx.exception.hint)
        self.assertIn("not your setup", NETWORK_HINT)

    def test_the_cli_prints_the_hint(self) -> None:
        h = Harness(self)
        expired = {
            "message": "Error validating access token",
            "type": "OAuthException",
            "code": 190,
            "error_subcode": 463,
        }
        h.graph.get_failures = [(400, {}, {"error": expired})]
        result = h.run("meta", "status")
        self.assertEqual(1, result.code)
        self.assertIn("error: hint: the access token expired", result.err)
        h.graph.get_failures = [(400, {}, {"error": {"message": "(#200) Permissions error", "code": 200}})]
        result = h.run("meta", "status")
        self.assertIn("check with: adops-guard doctor meta", result.err)

    def test_token_helper_failures(self) -> None:
        def keychain_miss(cmd, **kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                returncode=44, stdout="", stderr="security: The specified item could not be found.\n"
            )

        settings = MetaSettings(token_source="keychain", keychain_service="adops-guard-meta")
        with mock.patch("adops_guard.credentials.sys.platform", "darwin"):
            with self.assertRaises(CredentialError) as ctx:
                load_meta_token(settings, {}, keychain_miss)
        self.assertIn("keychain_service", ctx.exception.hint or "")

        def ssm_miss(cmd, **kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(returncode=254, stdout="", stderr="An error occurred (ParameterNotFound)\n")

        with self.assertRaises(CredentialError) as ctx:
            load_meta_token(MetaSettings(token_source="ssm", ssm_parameter="/x"), {}, ssm_miss)
        self.assertIn("ssm_parameter", ctx.exception.hint or "")


if __name__ == "__main__":
    unittest.main()
