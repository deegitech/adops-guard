"""In-memory fakes of the Google Ads REST API and the Meta Graph API, plus a CLI test harness.

Every identifier here is a placeholder. Fake credentials are assembled at run
time so no token-shaped literal sits in the source.
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adops_guard.cli import main
from adops_guard.redact import REDACTOR
from mock_http import MockServer, Recorded, Reply

DEV_TOKEN = "dev-" + "token-for-tests-only"
CLIENT_ID = "client-id-for-tests.apps.example"
CLIENT_SECRET = "GOCSPX-" + "not-a-real-client-secret"
REFRESH_TOKEN = "1//" + "not-a-real-refresh-token-0123"
ACCESS_TOKEN = "ya29." + "not-a-real-access-token-0123"
META_TOKEN = "EAA" + "BnotArealToken" + "0123456789"
CUSTOMER = "1234567890"
AD_ACCOUNT = "act_123456789012345"
SECRETS = (DEV_TOKEN, CLIENT_SECRET, REFRESH_TOKEN, ACCESS_TOKEN, META_TOKEN)


def camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part[:1].upper() + part[1:] for part in rest)


def dig(row: dict[str, Any], dotted: str) -> Any:
    value: Any = row
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(camel(part))
    return value


def put(row: dict[str, Any], dotted: str, value: Any) -> None:
    parts = [camel(p) for p in dotted.split(".")]
    for part in parts[:-1]:
        row = row.setdefault(part, {})
    row[parts[-1]] = value


def gaql_error(status: int, code_kind: str, code: str, message: str) -> Reply:
    return (
        status,
        {},
        {
            "error": {
                "code": status,
                "message": "Request contains an invalid argument.",
                "status": "INVALID_ARGUMENT" if status == 400 else "UNAVAILABLE",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                        "errors": [{"errorCode": {code_kind: code}, "message": message}],
                        "requestId": "test-request-id",
                    }
                ],
            }
        },
    )


class FakeGoogleAds:
    """Enough of the Google Ads REST API (OAuth, GAQL search, mutate) for the commands in this repo."""

    def __init__(self, version: str = "v25") -> None:
        self.version = version
        self.customer = {
            "resourceName": f"customers/{CUSTOMER}",
            "id": CUSTOMER,
            "descriptiveName": "Example Co",
            "currencyCode": "USD",
            "timeZone": "America/New_York",
            "status": "ENABLED",
        }
        self.campaigns: dict[str, dict[str, Any]] = {}
        self.budgets: dict[str, dict[str, Any]] = {}
        self.ad_groups: dict[str, dict[str, Any]] = {}
        self.ads: dict[str, dict[str, Any]] = {}
        self.conversions: list[dict[str, Any]] = []
        self.account_budgets: list[dict[str, Any]] = []
        self.recommendation_subscriptions: list[dict[str, Any]] = []
        self.cost_micros = 0
        self.metrics: dict[str, dict[str, Any]] = {}
        self.custom: list[tuple[re.Pattern[str], Any]] = []
        self.search_failures: list[Reply] = []
        self.list_failures: list[Reply] = []  # for customers:listAccessibleCustomers
        self.mutate_failures: list[Reply] = []
        self.validate_failures: list[Reply] = []
        self.ignore_updates = False
        self.apply_validate_only = False  # misbehave: apply validateOnly requests (the dry-run canary must catch it)
        self.mutations: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self.token_requests: list[Recorded] = []
        self.token_failures: list[Reply] = []
        self.token_expires_in = 3599
        self.page_size = 0
        self.accessible = [CUSTOMER]

    # ------------------------------------------------------------- fixtures
    def add_campaign(
        self,
        cid: str,
        name: str = "Campaign",
        status: str = "ENABLED",
        channel: str = "DEMAND_GEN",
        budget_micros: int = 30_000_000,
        shared: bool = False,
        refs: int = 1,
    ) -> None:
        rn = f"customers/{CUSTOMER}/campaignBudgets/9{cid}"
        self.budgets[rn] = {"amountMicros": budget_micros, "explicitlyShared": shared, "referenceCount": refs}
        self.campaigns[cid] = {"name": name, "status": status, "channel": channel, "budget": rn}

    def add_ad_group(
        self, agid: str, campaign: str, name: str = "Ad group", channels: dict[str, bool] | None = None
    ) -> None:
        self.ad_groups[agid] = {"name": name, "status": "ENABLED", "campaign": campaign, "channels": channels}

    def add_ad(self, agid: str, adid: str, name: str = "Ad", status: str = "ENABLED") -> None:
        self.ads[f"{agid}~{adid}"] = {"name": name, "status": status, "ad_group": agid}

    def on_query(self, pattern: str, response: Any) -> None:
        """``response``: rows (list), a reply tuple (status, headers, payload), or a callable(query)."""
        self.custom.append((re.compile(pattern, re.S), response))

    # ------------------------------------------------------------- rows
    def _campaign_row(self, cid: str) -> dict[str, Any]:
        c = self.campaigns[cid]
        b = self.budgets[c["budget"]]
        return {
            "campaign": {
                "resourceName": f"customers/{CUSTOMER}/campaigns/{cid}",
                "id": cid,
                "name": c["name"],
                "status": c["status"],
                "advertisingChannelType": c["channel"],
            },
            "campaignBudget": {
                "resourceName": c["budget"],
                "amountMicros": str(b["amountMicros"]),
                "explicitlyShared": b["explicitlyShared"],
                "referenceCount": str(b["referenceCount"]),
            },
            "metrics": self.metrics.get(cid, {}),
        }

    def _ad_group_row(self, agid: str) -> dict[str, Any]:
        g = self.ad_groups[agid]
        row = self._campaign_row(g["campaign"])
        group: dict[str, Any] = {
            "resourceName": f"customers/{CUSTOMER}/adGroups/{agid}",
            "id": agid,
            "name": g["name"],
            "status": g["status"],
        }
        if g["channels"] is not None:
            group["demandGenAdGroupSettings"] = {"channelControls": {"selectedChannels": dict(g["channels"])}}
        row["adGroup"] = group
        return row

    def rows(self, resource: str) -> list[dict[str, Any]]:
        if resource == "customer":
            return [{"customer": self.customer, "metrics": {"costMicros": str(self.cost_micros)}}]
        if resource == "campaign":
            return [self._campaign_row(cid) for cid in self.campaigns]
        if resource == "campaign_budget":
            return [
                {"campaignBudget": {"resourceName": rn, "amountMicros": str(b["amountMicros"])}}
                for rn, b in self.budgets.items()
            ]
        if resource == "ad_group":
            return [self._ad_group_row(agid) for agid in self.ad_groups]
        if resource == "ad_group_ad":
            out = []
            for key, ad in self.ads.items():
                row = self._ad_group_row(ad["ad_group"])
                row["adGroupAd"] = {
                    "resourceName": f"customers/{CUSTOMER}/adGroupAds/{key}",
                    "status": ad["status"],
                    "ad": {"id": key.split("~")[1], "name": ad["name"]},
                }
                out.append(row)
            return out
        if resource == "conversion_action":
            return [{"conversionAction": c} for c in self.conversions]
        if resource == "account_budget":
            return [{"accountBudget": b} for b in self.account_budgets]
        if resource == "recommendation_subscription":
            return [{"recommendationSubscription": r} for r in self.recommendation_subscriptions]
        if resource == "customer_client":
            return [{"customerClient": {"id": CUSTOMER, "descriptiveName": "Example Co", "level": "0", "status": "ENABLED",
                                        "currencyCode": "USD", "timeZone": "America/New_York"}}]  # fmt: skip
        raise ValueError(f"fake has no rows for {resource}")

    @staticmethod
    def _matches(row: dict[str, Any], where: str) -> bool:
        where = re.sub(r"segments\.date BETWEEN '[^']*' AND '[^']*'", "", where)
        for cond in [c.strip() for c in re.split(r"\s+AND\s+", where) if c.strip()]:
            m = re.fullmatch(r"([\w.]+)\s*(=|!=)\s*(?:'((?:[^'\\]|\\.)*)'|(\d+))", cond)
            if not m:
                raise ValueError(f"fake cannot evaluate condition {cond!r}")
            field_name, op, text, number = m.groups()
            want = number if number is not None else re.sub(r"\\(.)", r"\1", text)
            got = str(dig(row, field_name) if dig(row, field_name) is not None else "")
            if (got == want) != (op == "="):
                return False
        return True

    @staticmethod
    def _project(row: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in fields:
            value = dig(row, name)
            if value in (None, False, 0, "", "0", {}, []):
                continue  # proto3 JSON leaves out default values
            put(out, name, copy.deepcopy(value))
        return out

    def search(self, query: str) -> Reply:
        self.queries.append(query)
        if self.search_failures:
            return self.search_failures.pop(0)
        for pattern, response in self.custom:
            if pattern.search(query):
                result = response(query) if callable(response) else response
                if isinstance(result, tuple):
                    return result
                return 200, {}, {"results": result}
        m = re.match(
            r"\s*SELECT\s+(.*?)\s+FROM\s+(\w+)(?:\s+WHERE\s+(.*?))?(?:\s+ORDER\s+BY\s+.*?)?(?:\s+LIMIT\s+\d+)?\s*$",
            query,
            re.S | re.I,
        )
        if not m:
            raise ValueError(f"fake cannot parse {query!r}")
        fields = [f.strip() for f in m.group(1).split(",")]
        rows = [self._project(r, fields) for r in self.rows(m.group(2)) if self._matches(r, m.group(3) or "")]
        return 200, {}, {"results": rows}

    # ------------------------------------------------------------- mutate
    def _target(self, service: str, rn: str) -> dict[str, Any] | None:
        ident = rn.rsplit("/", 1)[-1]
        if service == "campaignBudgets":
            return self.budgets.get(rn)
        if service == "campaigns":
            return self.campaigns.get(ident)
        if service == "adGroups":
            return self.ad_groups.get(ident)
        if service == "adGroupAds":
            return self.ads.get(ident)
        return None

    def mutate(self, service: str, body: dict[str, Any]) -> Reply:
        validate = bool(body.get("validateOnly"))
        self.mutations.append({"service": service, "validate": validate, "body": body})
        queue = self.validate_failures if validate else self.mutate_failures
        if queue:
            return queue.pop(0)
        results = []
        for op in body["operations"]:
            if "update" in op:
                update = op["update"]
                target = self._target(service, update["resourceName"])
                if target is None:
                    return gaql_error(400, "mutateError", "RESOURCE_NOT_FOUND", "Resource was not found.")
                if (not validate or self.apply_validate_only) and not self.ignore_updates:
                    for path in op["updateMask"].split(","):
                        value = dig(update, path)
                        if service == "campaignBudgets" and path == "amount_micros":
                            target["amountMicros"] = int(value)
                        elif path == "status":
                            target["status"] = value
                        elif path.startswith("demand_gen_ad_group_settings.channel_controls.selected_channels."):
                            target.setdefault("channels", {})
                            if target["channels"] is None:
                                target["channels"] = {}
                            target["channels"][camel(path.rsplit(".", 1)[1])] = bool(value)
                        else:
                            raise ValueError(f"fake cannot update {path}")
                results.append({"resourceName": update["resourceName"]})
            elif "create" in op and service == "conversionActions":
                if not validate:
                    n = str(len(self.conversions) + 1)
                    created = dict(op["create"])
                    created.pop("valueSettings", None)
                    created.update(
                        resourceName=f"customers/{CUSTOMER}/conversionActions/{n}",
                        id=n,
                        tagSnippets=[
                            {
                                "type": "WEBPAGE",
                                "pageFormat": "HTML",
                                "globalSiteTag": "gtag('config', 'AW-000000000');",
                                "eventSnippet": "gtag('event', 'conversion', {'send_to': 'AW-000000000/FAKE_LABEL_1'});",
                            }
                        ],
                    )
                    self.conversions.append(created)
                    results.append({"resourceName": created["resourceName"]})
            else:
                raise ValueError(f"fake cannot run {op}")
        return 200, {}, ({} if validate else {"results": results})

    # ------------------------------------------------------------- HTTP entry point
    def __call__(self, req: Recorded) -> Reply:
        if req.path == "/token":
            self.token_requests.append(req)
            if self.token_failures:
                return self.token_failures.pop(0)
            form = req.form()
            if form.get("refresh_token") != REFRESH_TOKEN or form.get("client_secret") != CLIENT_SECRET:
                return 400, {}, {"error": "invalid_grant", "error_description": "Bad Request"}
            return 200, {}, {"access_token": ACCESS_TOKEN, "expires_in": self.token_expires_in, "token_type": "Bearer"}
        m = re.fullmatch(r"/(v\d+)/(.*)", req.path)
        if not m:
            return 404, {}, {"error": {"code": 404, "message": "unknown path"}}
        version, rest = m.groups()
        if version != self.version:
            return 404, {}, {"error": {"code": 404, "message": "version not found", "status": "NOT_FOUND"}}
        if (
            req.headers.get("authorization") != f"Bearer {ACCESS_TOKEN}"
            or req.headers.get("developer-token", DEV_TOKEN) != DEV_TOKEN
        ):  # a developer token is optional since September 2026, but a wrong one still fails here
            return 401, {}, {"error": {"code": 401, "message": "unauthenticated", "status": "UNAUTHENTICATED"}}
        if rest == "customers:listAccessibleCustomers":
            if self.list_failures:
                return self.list_failures.pop(0)
            return 200, {}, {"resourceNames": [f"customers/{c}" for c in self.accessible]}
        m = re.fullmatch(r"customers/(\d+)/googleAds:search", rest)
        if m:
            status, headers, payload = self.search(req.json()["query"])
            if status == 200 and self.page_size and len(payload["results"]) > self.page_size:
                return self._page(req, payload["results"])
            return status, headers, payload
        m = re.fullmatch(r"customers/(\d+)/(\w+):mutate", rest)
        if m:
            return self.mutate(m.group(2), req.json())
        return 404, {}, {"error": {"code": 404, "message": f"no route {rest}"}}

    def _page(self, req: Recorded, results: list[dict[str, Any]]) -> Reply:
        start = int(req.json().get("pageToken") or 0)
        chunk = results[start : start + self.page_size]
        payload: dict[str, Any] = {"results": chunk}
        if start + self.page_size < len(results):
            payload["nextPageToken"] = str(start + self.page_size)
        return 200, {}, payload


GRAPH_FIELDS = {
    "campaign": {"id", "name", "status", "effective_status", "account_id", "objective", "daily_budget", "lifetime_budget", "spend_cap"},
    "adset": {"id", "name", "status", "effective_status", "account_id", "campaign_id", "daily_budget", "lifetime_budget", "optimization_goal"},
    "ad": {"id", "name", "status", "effective_status", "account_id", "campaign_id", "adset_id"},
    "video": {"id", "title", "length", "status", "is_instagram_eligible"},
    "app": {"id", "name", "category", "privacy_policy_url", "icon_url", "supported_platforms", "object_store_urls"},
}  # fmt: skip
NODE_NAMES = {
    "campaign": "AdCampaignGroup",
    "adset": "AdCampaign",
    "ad": "AdGroup",
    "video": "AdVideo",
    "app": "Application",
}
META_APP_ID = "222222222222222"  # a placeholder Meta app id
APP_STORE_ID = "1234567890"  # a placeholder App Store id
IG_USER_ID = "17841401234567890"  # a placeholder Instagram account id


def _split_fields(fields: str) -> list[str]:
    names, depth, current = [], 0, ""
    for ch in fields:
        depth += ch == "{"
        depth -= ch == "}"
        if ch == "," and depth == 0:
            names.append(current)
            current = ""
        else:
            current += ch
    names.append(current)
    return [name.strip() for name in names if name.strip()]


def top_level_fields(fields: str) -> list[str]:
    """``id,name,instagram_business_account{id,username}`` -> ``['id', 'name', 'instagram_business_account']``."""
    return [name.split("{", 1)[0].strip() for name in _split_fields(fields)]


def select(item: dict[str, Any], fields: str) -> dict[str, Any]:
    """The fields of ``item`` a request asked for. Like the Graph API, a field that points at another node (a
    dict with an id) comes back as its id only, unless the request names its fields: ``field{id,username}``."""
    out: dict[str, Any] = {}
    for spec in _split_fields(fields):
        name, _, sub = spec.partition("{")
        value = item.get(name.strip())
        if value is None:
            continue
        if isinstance(value, dict) and sub:
            wanted = [part.strip() for part in sub.rstrip("}").split(",") if part.strip()]
            value = {key: value[key] for key in wanted if key in value}
        elif isinstance(value, dict) and "id" in value:
            value = {"id": value["id"]}
        out[name.strip()] = value
    return out


class FakeGraph:
    """Enough of the Graph API (objects, edges, insights, usage headers) for the Meta commands."""

    def __init__(self, version: str = "v26.0") -> None:
        self.version = version
        self.account = {
            "id": AD_ACCOUNT,
            "account_id": AD_ACCOUNT.removeprefix("act_"),
            "name": "Example Ads",
            "account_status": 1,
            "currency": "USD",
            "timezone_name": "America/Los_Angeles",
            "amount_spent": "12345",
            "spend_cap": "0",
            "min_daily_budget": "100",
            "min_campaign_group_spend_cap": "10000",
            "is_prepay_account": False,
        }
        self.app = {
            "id": META_APP_ID,
            "name": "Example App",
            "category": "Games",
            "privacy_policy_url": "https://example.com/privacy",
            "icon_url": "https://example.com/icon.png",
            "supported_platforms": ["IPHONE", "IPAD"],
            "object_store_urls": {"itunes": f"https://apps.apple.com/app/id{APP_STORE_ID}"},
        }
        self.app_fields = set(GRAPH_FIELDS["app"])  # fields this fake "API version" knows on the app node
        self.pages = [
            {"id": "333333333333333", "name": "Example Page",
             "instagram_business_account": {"id": IG_USER_ID, "username": "example_brand"}},
        ]  # fmt: skip
        self.advertisable_apps = [
            {"id": META_APP_ID, "name": "Example App", "supported_platforms": ["IPHONE", "IPAD"],
             "object_store_urls": {"itunes": f"https://apps.apple.com/app/id{APP_STORE_ID}"}},
        ]  # fmt: skip
        self.objects: dict[str, dict[str, Any]] = {}
        self.insights: list[dict[str, Any]] = []
        self.usage_headers: dict[str, str] = {}
        self.get_failures: list[Reply] = []
        self.post_failures: list[Reply] = []
        self.validate_failures: list[Reply] = []
        self.ignore_posts = False
        self.apply_validate_only = False  # misbehave: apply validate_only requests (the dry-run canary must catch it)
        self.posts: list[dict[str, Any]] = []
        self.gets: list[Recorded] = []
        self.permissions = [
            {"permission": "ads_read", "status": "granted"},
            {"permission": "ads_management", "status": "granted"},
        ]

    def add(self, kind: str, oid: str, **fields: Any) -> None:
        base = {"id": oid, "name": f"{kind} {oid}", "status": "PAUSED", "effective_status": "PAUSED"}
        if kind != "video":
            base["account_id"] = self.account["account_id"]
        base.update(fields)
        self.objects[oid] = {"_type": kind, **base}

    def _error(
        self, status: int, code: int, message: str, subcode: int | None = None, headers: dict[str, str] | None = None
    ) -> Reply:
        err: dict[str, Any] = {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": "TRACE"}
        if subcode:
            err["error_subcode"] = subcode
        return status, headers or dict(self.usage_headers), {"error": err}

    def _fields(self, obj: dict[str, Any], kind: str, wanted: str, allowed: set[str] | None = None) -> Reply:
        allowed = allowed if allowed is not None else GRAPH_FIELDS.get(kind)
        out: dict[str, Any] = {}
        for name in top_level_fields(wanted):
            if allowed is not None and name not in allowed:
                return self._error(
                    400, 100, f"(#100) Tried accessing nonexisting field ({name}) on node type ({NODE_NAMES[kind]})"
                )
            if obj.get(name) is not None:
                out[name] = obj[name]
        return 200, dict(self.usage_headers), out

    def _list(self, items: list[dict[str, Any]], query: dict[str, list[str]], fields: str) -> Reply:
        limit = int((query.get("limit") or ["100"])[0])
        start = int((query.get("after") or ["0"])[0])
        chunk = items[start : start + limit]
        data = [select(item, fields) for item in chunk]
        payload: dict[str, Any] = {"data": data}
        if start + limit < len(items):
            payload["paging"] = {
                "cursors": {"after": str(start + limit)},
                "next": "https://graph.facebook.com/next-page",
            }
        return 200, dict(self.usage_headers), payload

    def get(self, req: Recorded, path: str) -> Reply:
        self.gets.append(req)
        if self.get_failures:
            return self.get_failures.pop(0)
        fields = (req.query.get("fields") or [""])[0]
        if path == "me":
            return 200, dict(self.usage_headers), {"id": "111111111111111", "name": "Test User"}
        if path == "me/permissions":
            return 200, dict(self.usage_headers), {"data": self.permissions}
        if path == AD_ACCOUNT:
            return self._fields(self.account, "account", fields)
        if path == "app":
            return self._fields(self.app, "app", fields, self.app_fields)
        if path == "me/accounts":
            return self._list(self.pages, req.query, fields)
        if path == f"{AD_ACCOUNT}/advertisable_applications":
            return self._list(self.advertisable_apps, req.query, fields)
        if path == f"{AD_ACCOUNT}/campaigns":
            return self._list([o for o in self.objects.values() if o["_type"] == "campaign"], req.query, fields)
        if path == f"{AD_ACCOUNT}/insights":
            return self._list(self.insights, req.query, fields)
        m = re.fullmatch(r"(\d+)/(adsets|ads)", path)
        if m:
            kind = "adset" if m.group(2) == "adsets" else "ad"
            items = [o for o in self.objects.values() if o["_type"] == kind and o.get("campaign_id") == m.group(1)]
            return self._list(items, req.query, fields)
        obj = self.objects.get(path)
        if obj is None:
            return self._error(400, 100, f"(#100) Unsupported get request. Object with ID '{path}' does not exist")
        return self._fields(obj, obj["_type"], fields)

    def post(self, req: Recorded, path: str) -> Reply:
        form = req.form()
        validate = "validate_only" in form.get("execution_options", "")
        self.posts.append({"path": path, "form": form, "validate": validate})
        queue = self.validate_failures if validate else self.post_failures
        if queue:
            return queue.pop(0)
        obj = self.objects.get(path)
        if obj is None:
            return self._error(400, 100, "(#100) Unsupported post request.")
        if (not validate or self.apply_validate_only) and not self.ignore_posts:
            for key, value in form.items():
                if key != "execution_options":
                    obj[key] = value
        return 200, dict(self.usage_headers), {"success": True}

    def __call__(self, req: Recorded) -> Reply:
        m = re.fullmatch(r"/(v\d+\.\d)/(.*)", req.path)
        if not m or m.group(1) != self.version:
            return self._error(400, 2635, "unknown version")
        if "access_token" in req.raw_path:
            raise AssertionError("access token found in a URL")
        if req.headers.get("authorization") != f"Bearer {META_TOKEN}":
            return self._error(400, 190, "Invalid OAuth access token.")
        return self.get(req, m.group(2)) if req.method == "GET" else self.post(req, m.group(2))


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err

    def json(self) -> Any:
        return json.loads(self.out)


class Harness:
    """Runs the real CLI against a local mock server, with a temp config and state directory."""

    def __init__(
        self, test: unittest.TestCase, *, google: FakeGoogleAds | None = None, graph: FakeGraph | None = None
    ) -> None:
        self.test = test
        self.google = google or FakeGoogleAds()
        self.graph = graph or FakeGraph()
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.server = MockServer(self._route).start()
        test.addCleanup(self.server.stop)
        test.addCleanup(REDACTOR.forget_all)
        self.env = {
            "GOOGLE_ADS_DEVELOPER_TOKEN": DEV_TOKEN,
            "GOOGLE_ADS_CLIENT_ID": CLIENT_ID,
            "GOOGLE_ADS_CLIENT_SECRET": CLIENT_SECRET,
            "GOOGLE_ADS_REFRESH_TOKEN": REFRESH_TOKEN,
            "META_ACCESS_TOKEN": META_TOKEN,
            "HOME": str(self.dir),
            # The config below points the clients at the local mock server; the tool refuses that without this.
            "ADOPS_GUARD_TEST_ENDPOINTS": "1",
        }
        url = self.server.url
        self.config: dict[str, dict[str, str]] = {
            "general": {"state_dir": "state"},
            "google": {
                "customer_id": "123-456-7890",
                "api_base_url": url,
                "oauth_token_url": f"{url}/token",
                "max_daily_budget": "50.00",
            },
            "meta": {
                "ad_account_id": AD_ACCOUNT,
                "graph_base_url": url,
                "max_daily_budget": "50.00",
                "max_lifetime_budget": "500.00",
                "max_spend_cap": "1000.00",
            },
        }
        self.now = datetime(2030, 5, 3, 12, 0, tzinfo=timezone.utc)
        self.clock = 0.0
        self.sleeps: list[float] = []
        self.runner: Any = None

    def _route(self, req: Recorded) -> Reply:
        if re.match(r"/v\d+\.\d/", req.path):
            return self.graph(req)
        return self.google(req)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds

    @property
    def config_path(self) -> Path:
        return self.dir / "adops-guard.ini"

    def write_config(self) -> None:
        lines = []
        for section, values in self.config.items():
            lines.append(f"[{section}]")
            lines += [f"{k} = {v}" for k, v in values.items()]
        self.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(self.config_path, 0o600)  # config files other users could change are refused

    def run(self, *argv: str, json_mode: bool = False) -> Result:
        self.write_config()
        out, err = io.StringIO(), io.StringIO()
        args = ["--config", str(self.config_path), *(["--json"] if json_mode else []), *argv]
        code = main(
            args,
            env=self.env,
            stdout=out,
            stderr=err,
            sleep=self._sleep,
            now=lambda: self.now,
            monotonic=lambda: self.clock,
            runner=self.runner,
            cwd=self.dir,
        )
        self.test.assertEqual([], self.server.errors, "the mock server raised")
        result = Result(int(code), out.getvalue(), err.getvalue())
        for secret in SECRETS:
            self.test.assertNotIn(secret, result.text, "a secret reached the output")
        return result

    @property
    def state_dir(self) -> Path:
        return self.dir / "state"

    def journal(self) -> list[dict[str, Any]]:
        path = self.state_dir / "journal.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def state(self) -> dict[str, Any]:
        path = self.state_dir / "state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
