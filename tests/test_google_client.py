from __future__ import annotations

import unittest

from adops_guard.credentials import GoogleCredentials
from adops_guard.errors import AmbiguousWrite, ApiError, ConfigError, CredentialError, NetworkError, RateLimited
from adops_guard.google.client import GoogleAdsClient, normalize_customer_id
from adops_guard.redact import REDACTOR, Secret
from fakes import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    CUSTOMER,
    DEV_TOKEN,
    REFRESH_TOKEN,
    FakeGoogleAds,
    gaql_error,
)
from mock_http import MockServer


def credentials(refresh: str = REFRESH_TOKEN, login: str | None = None, dev: bool = True) -> GoogleCredentials:
    return GoogleCredentials(
        developer_token=Secret(DEV_TOKEN) if dev else None,
        client_id=Secret(CLIENT_ID),
        client_secret=Secret(CLIENT_SECRET),
        refresh_token=Secret(refresh),
        login_customer_id=login,
        source="test",
    )


class GoogleClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeGoogleAds()
        self.fake.add_campaign("11111111111", "Alpha")
        self.fake.add_campaign("11111111112", "Beta")
        self.server = MockServer(self.fake).start()
        self.addCleanup(self.server.stop)
        self.addCleanup(REDACTOR.forget_all)
        self.sleeps: list[float] = []

    def client(self, **kwargs) -> GoogleAdsClient:  # type: ignore[no-untyped-def]
        base = dict(
            api_base_url=self.server.url,
            oauth_token_url=self.server.url + "/token",
            sleep=self.sleeps.append,
            allow_test_endpoints=True,
        )
        base.update(kwargs)
        creds = base.pop("creds", None) or credentials()
        return GoogleAdsClient(creds, "123-456-7890", **base)

    def test_customer_id_normalisation(self) -> None:
        self.assertEqual(CUSTOMER, normalize_customer_id("123-456-7890"))
        for bad in ("123", "12345678901", "abc-def-ghij", ""):
            with self.assertRaises(ConfigError):
                normalize_customer_id(bad)

    def test_refresh_token_goes_in_the_body_and_the_access_token_is_cached(self) -> None:
        client = self.client()
        client.search("SELECT campaign.id FROM campaign")
        client.search("SELECT campaign.id FROM campaign")
        self.assertEqual(1, len(self.fake.token_requests))
        token_req = self.fake.token_requests[0]
        self.assertNotIn(REFRESH_TOKEN, token_req.raw_path)
        self.assertEqual(REFRESH_TOKEN, token_req.form()["refresh_token"])
        api = [r for r in self.server.requests if r.path.startswith("/v25/")]
        for req in api:
            self.assertEqual(f"Bearer {ACCESS_TOKEN}", req.headers["authorization"])
            self.assertEqual(DEV_TOKEN, req.headers["developer-token"])
            self.assertNotIn(ACCESS_TOKEN, req.raw_path)
            self.assertNotIn("login-customer-id", req.headers)

    def test_no_developer_token_header_without_a_developer_token(self) -> None:
        self.client(creds=credentials(dev=False)).search("SELECT campaign.id FROM campaign")
        api = [r for r in self.server.requests if r.path.startswith("/v25/")]
        self.assertTrue(api)
        for req in api:
            self.assertNotIn("developer-token", req.headers)

    def test_login_customer_id_header(self) -> None:
        client = self.client(login_customer_id="999-888-7777")
        client.search("SELECT campaign.id FROM campaign")
        self.assertEqual("9998887777", self.server.requests[-1].headers["login-customer-id"])

    def test_search_follows_page_tokens(self) -> None:
        self.fake.page_size = 1
        rows = self.client().search("SELECT campaign.id, campaign.name FROM campaign")
        self.assertEqual(["Alpha", "Beta"], [r["campaign"]["name"] for r in rows])
        searches = [r for r in self.server.requests if r.path.endswith("googleAds:search")]
        self.assertEqual(2, len(searches))
        self.assertEqual("1", searches[1].json()["pageToken"])

    def test_reads_retry_on_5xx_and_429(self) -> None:
        self.fake.search_failures = [gaql_error(503, "internalError", "TRANSIENT_ERROR", "try again"),
                                     gaql_error(429, "quotaError", "RESOURCE_EXHAUSTED", "Too many requests")]  # fmt: skip
        rows = self.client().search("SELECT campaign.id FROM campaign")
        self.assertEqual(2, len(rows))
        self.assertEqual(2, len(self.sleeps))

    def test_reads_give_up_after_max_attempts(self) -> None:
        self.fake.search_failures = [gaql_error(429, "quotaError", "RESOURCE_EXHAUSTED", "Too many requests")] * 3
        with self.assertRaises(RateLimited):
            self.client().search("SELECT campaign.id FROM campaign")

    def test_writes_are_not_retried_and_5xx_is_ambiguous(self) -> None:
        self.fake.mutate_failures = [gaql_error(500, "internalError", "INTERNAL_ERROR", "Internal error encountered.")]
        op = {
            "update": {"resourceName": f"customers/{CUSTOMER}/campaigns/11111111111", "status": "PAUSED"},
            "updateMask": "status",
        }
        with self.assertRaises(AmbiguousWrite) as ctx:
            self.client().mutate("campaigns", [op], validate_only=False)
        self.assertIn("re-run", ctx.exception.hint or "")
        self.assertEqual(1, len([m for m in self.fake.mutations if not m["validate"]]))
        self.assertEqual([], self.sleeps)

    def test_validate_only_is_retried(self) -> None:
        self.fake.validate_failures = [gaql_error(503, "internalError", "TRANSIENT_ERROR", "try again")]
        op = {
            "update": {"resourceName": f"customers/{CUSTOMER}/campaigns/11111111111", "status": "PAUSED"},
            "updateMask": "status",
        }
        self.assertEqual({}, self.client().mutate("campaigns", [op], validate_only=True))
        self.assertEqual(2, len(self.fake.mutations))

    def test_error_description_has_codes_location_and_request_id(self) -> None:
        def bad(query: str):  # type: ignore[no-untyped-def]
            status, headers, payload = gaql_error(400, "queryError", "UNRECOGNIZED_FIELD", "Unrecognized field.")
            payload["error"]["details"][0]["errors"][0]["location"] = {
                "fieldPathElements": [{"fieldName": "operations", "index": 0}, {"fieldName": "create"}]
            }
            return status, headers, payload

        self.fake.on_query("bogus", bad)
        with self.assertRaises(ApiError) as ctx:
            self.client().search("SELECT campaign.bogus FROM campaign")
        text = str(ctx.exception)
        self.assertIn("queryError=UNRECOGNIZED_FIELD", text)
        self.assertIn("operations[0].create", text)
        self.assertIn("requestId test-request-id", text)
        self.assertIn("API version", ctx.exception.hint or "")

    def test_invalid_grant_is_a_credential_error_without_secrets(self) -> None:
        client = self.client(creds=credentials(refresh="1//" + "revoked-refresh-token-xyz"))
        with self.assertRaises(CredentialError) as ctx:
            client.search("SELECT campaign.id FROM campaign")
        self.assertIn("invalid_grant", str(ctx.exception))
        self.assertIn("7 days", ctx.exception.hint or "")
        self.assertNotIn("revoked-refresh-token-xyz", str(ctx.exception))

    def test_other_token_refusals_get_a_different_hint(self) -> None:
        self.fake.token_failures = [(401, {}, {"error": "invalid_client", "error_description": "Unauthorized"})]
        with self.assertRaises(CredentialError) as ctx:
            self.client().search("SELECT campaign.id FROM campaign")
        self.assertIn("invalid_client", str(ctx.exception))
        self.assertNotIn("7 days", ctx.exception.hint or "")

    def test_token_endpoint_outage_is_temporary_not_a_credential_error(self) -> None:
        self.fake.token_failures = [(503, {}, {"error": "temporarily_unavailable"})]
        self.assertEqual(2, len(self.client().search("SELECT campaign.id FROM campaign")))  # retried
        self.assertEqual([2], self.sleeps)
        self.fake.token_failures = [(503, {}, {}), (429, {}, {}), (500, {}, {})]
        with self.assertRaises(NetworkError) as ctx:
            self.client().search("SELECT campaign.id FROM campaign")
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, CredentialError)

    def test_token_outage_before_a_write_is_not_an_ambiguous_write(self) -> None:
        self.fake.token_failures = [(503, {}, {})] * 3
        op = {
            "update": {"resourceName": f"customers/{CUSTOMER}/campaigns/11111111111", "status": "PAUSED"},
            "updateMask": "status",
        }
        with self.assertRaises(NetworkError) as ctx:
            self.client().mutate("campaigns", [op], validate_only=False)
        self.assertNotIsInstance(ctx.exception, AmbiguousWrite)
        self.assertEqual([], self.fake.mutations, "nothing may be sent without a token")

    def test_a_200_that_is_not_json(self) -> None:
        self.fake.search_failures = [(200, {}, "<html>proxy</html>")] * 3
        with self.assertRaises(ApiError):
            self.client().search("SELECT campaign.id FROM campaign")  # a read never turns into "no rows"
        self.fake.mutate_failures = [(200, {}, "<html>proxy</html>")]
        op = {
            "update": {"resourceName": f"customers/{CUSTOMER}/campaigns/11111111111", "status": "PAUSED"},
            "updateMask": "status",
        }
        self.assertEqual({}, self.client().mutate("campaigns", [op], validate_only=False))  # read-back decides

    def test_expired_access_token_is_refreshed_once(self) -> None:
        responses = iter([(401, {}, {"error": {"code": 401, "status": "UNAUTHENTICATED"}})])
        original = self.fake.search

        def flaky(query: str):  # type: ignore[no-untyped-def]
            try:
                return next(responses)
            except StopIteration:
                return original(query)

        self.fake.search = flaky  # type: ignore[method-assign]
        self.assertEqual(2, len(self.client().search("SELECT campaign.id FROM campaign")))
        self.assertEqual(2, len(self.fake.token_requests))

    def test_auto_version_probes_newest_first(self) -> None:
        client = self.client(api_version="auto")
        self.assertEqual("v25", client.api_version())
        probes = [r.path for r in self.server.requests if r.path.endswith("listAccessibleCustomers")]
        self.assertEqual("/v30/customers:listAccessibleCustomers", probes[0])
        self.assertEqual("/v25/customers:listAccessibleCustomers", probes[-1])

    def test_bad_version_and_endpoints(self) -> None:
        with self.assertRaises(ConfigError):
            self.client(api_version="25")
        with self.assertRaises(ConfigError):
            self.client(api_base_url="https://example.com")
        with self.assertRaises(ConfigError):
            self.client(oauth_token_url="http://oauth2.googleapis.com/token")
        with self.assertRaises(ConfigError):  # the mock server itself, without the test flag
            self.client(allow_test_endpoints=False)


if __name__ == "__main__":
    unittest.main()
