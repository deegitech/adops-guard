from __future__ import annotations

import json
import unittest

from adops_guard.errors import AmbiguousWrite, ApiError, ConfigError, RateLimited
from adops_guard.meta.client import MetaClient, drop_field, encode_params
from adops_guard.redact import REDACTOR, Secret
from fakes import AD_ACCOUNT, META_TOKEN, FakeGraph
from mock_http import MockServer


class MetaClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = FakeGraph()
        self.graph.add("campaign", "120000000000000001", daily_budget="2500")
        self.graph.add("adset", "120000000000000002", campaign_id="120000000000000001")
        self.server = MockServer(self.graph).start()
        self.addCleanup(self.server.stop)
        self.addCleanup(REDACTOR.forget_all)
        self.sleeps: list[float] = []

    def client(self, **kwargs) -> MetaClient:  # type: ignore[no-untyped-def]
        return MetaClient(
            Secret(META_TOKEN), base_url=self.server.url, sleep=self.sleeps.append, allow_test_endpoints=True, **kwargs
        )

    def test_token_only_in_the_authorization_header(self) -> None:
        client = self.client()
        client.get("120000000000000001", {"fields": "id,name"})
        client.post("120000000000000001", {"status": "ACTIVE"}, validate_only=True)
        for req in self.server.requests:
            self.assertEqual(f"Bearer {META_TOKEN}", req.headers["authorization"])
            self.assertNotIn(META_TOKEN, req.raw_path)
            self.assertNotIn(META_TOKEN.encode(), req.body)

    def test_validate_only_sends_execution_options(self) -> None:
        self.client().post("120000000000000001", {"status": "ACTIVE"}, validate_only=True)
        form = self.server.requests[-1].form()
        self.assertEqual(["validate_only"], json.loads(form["execution_options"]))
        self.assertEqual("PAUSED", self.graph.objects["120000000000000001"]["status"])

    def test_http_200_with_an_error_body_is_an_error(self) -> None:
        self.graph.post_failures = [(200, {}, {"error": {"message": "This request requires the user to take a pending action",
                                                         "type": "OAuthException", "code": 31, "error_subcode": 3858385}})]  # fmt: skip
        with self.assertRaises(ApiError) as ctx:
            self.client().post("120000000000000001", {"status": "ACTIVE"})
        self.assertIn("security check", ctx.exception.hint or "")

    def test_known_subcodes_get_hints(self) -> None:
        self.graph.validate_failures = [
            (400, {}, {"error": {"message": "Invalid parameter", "code": 100, "error_subcode": 1487810}})
        ]
        with self.assertRaises(ApiError) as ctx:
            self.client().post("120000000000000001", {"status": "ACTIVE"}, validate_only=True)
        self.assertIn("App Promotion", ctx.exception.hint or "")
        self.assertIn("code 100/1487810", str(ctx.exception))

    def test_usage_guard_stops_before_sending(self) -> None:
        self.graph.usage_headers = {
            "X-App-Usage": json.dumps({"call_count": 20, "total_time": 10, "total_cputime": 5}),
            "X-Business-Use-Case-Usage": json.dumps(
                {"999": [{"type": "ads_management", "call_count": 86, "estimated_time_to_regain_access": 12}]}
            ),  # fmt: skip
        }
        client = self.client()
        client.get(AD_ACCOUNT, {"fields": "id"})
        self.assertEqual(86, client.usage_percent())
        self.assertEqual(12, client.regain_minutes())
        sent = len(self.server.requests)
        with self.assertRaises(RateLimited) as ctx:
            client.get(AD_ACCOUNT, {"fields": "id"})
        self.assertEqual(sent, len(self.server.requests), "no request may be sent above the threshold")
        self.assertIn("12 min", ctx.exception.hint or "")

    def test_usage_threshold_is_configurable_and_reads_all_headers(self) -> None:
        self.graph.usage_headers = {
            "X-Ad-Account-Usage": json.dumps({"acc_id_util_pct": 61.5}),
            "X-FB-Ads-Insights-Throttle": json.dumps({"app_id_util_pct": 12, "acc_id_util_pct": 40}),
        }
        client = self.client(usage_stop=60)
        client.get(AD_ACCOUNT, {"fields": "id"})
        self.assertEqual(61.5, client.usage_percent())
        with self.assertRaises(RateLimited):
            client.get(AD_ACCOUNT, {"fields": "id"})

    def test_rate_limit_codes_are_not_retried(self) -> None:
        self.graph.get_failures = [(400, {}, {"error": {"message": "User request limit reached", "code": 17}})]
        with self.assertRaises(RateLimited):
            self.client().get(AD_ACCOUNT, {"fields": "id"})
        self.assertEqual([], self.sleeps)

    def test_transient_reads_are_retried(self) -> None:
        self.graph.get_failures = [(500, {}, {"error": {"message": "temporary", "code": 2, "is_transient": True}})]
        self.assertEqual(AD_ACCOUNT, self.client().get(AD_ACCOUNT, {"fields": "id"})["id"])
        self.assertEqual([5.0], self.sleeps)

    def test_failed_writes_are_ambiguous_and_not_retried(self) -> None:
        self.graph.post_failures = [(503, {}, {"error": {"message": "Service temporarily unavailable", "code": 2}})]
        with self.assertRaises(AmbiguousWrite):
            self.client().post("120000000000000001", {"status": "ACTIVE"})
        self.assertEqual(1, len([p for p in self.graph.posts if not p["validate"]]))

    def test_get_fields_drops_fields_this_version_does_not_know(self) -> None:
        result = self.client().get_fields("120000000000000002", "id,name,spend_cap,adset_id,campaign_id")
        self.assertEqual(["spend_cap", "adset_id"], result["_dropped"])
        self.assertEqual("120000000000000001", result["campaign_id"])

    def test_get_all_uses_cursors_not_next_urls(self) -> None:
        for i in range(3, 8):
            self.graph.add("campaign", f"12000000000000000{i}")
        items = self.client().get_all(f"{AD_ACCOUNT}/campaigns", {"fields": "id", "limit": 2})
        self.assertEqual(6, len(items))
        paths = [r.raw_path for r in self.server.requests]
        self.assertTrue(all("next-page" not in p for p in paths))
        self.assertTrue(any("after=2" in p for p in paths))

    def test_paths_and_versions_are_validated(self) -> None:
        with self.assertRaises(ConfigError):
            self.client().get("me?access_token=x")
        with self.assertRaises(ConfigError):
            self.client().get("../other")
        with self.assertRaises(ConfigError):
            MetaClient(Secret(META_TOKEN), api_version="26")
        with self.assertRaises(ConfigError):
            MetaClient(Secret(META_TOKEN), base_url="https://graph.facebook.com.evil.example")
        with self.assertRaises(ConfigError):  # the mock server itself, without the test flag
            MetaClient(Secret(META_TOKEN), base_url=self.server.url)

    def test_helpers(self) -> None:
        self.assertEqual("id,name", drop_field("id,bad{a,b},name", "bad"))
        self.assertEqual("id,creative{id,name}", drop_field("id,creative{id,name},x", "x"))
        self.assertEqual(
            {"a": '["x"]', "b": "true", "c": "5"}, encode_params({"a": ["x"], "b": True, "c": 5, "d": None})
        )


if __name__ == "__main__":
    unittest.main()
