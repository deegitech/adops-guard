"""Google Ads commands: accounts, status, report, budget, pause/enable, channel controls, conversion actions."""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from adops_guard.changes import Change, execute, settle_noop
from adops_guard.context import Context
from adops_guard.credentials import load_google_credentials
from adops_guard.dates import account_now, resolve_range
from adops_guard.errors import ApiError, ConfigError, ExitCode, GuardRefused, RateLimited
from adops_guard.google.client import GoogleAdsClient
from adops_guard.google.gaql import as_int, between, gaql_id, gaql_string, parse_ad_ref
from adops_guard.guards import check_ceiling
from adops_guard.money import fmt_amount, from_micros, parse_amount, to_micros
from adops_guard.output import fmt_int, fmt_pct

CUSTOMER_QUERY = (
    "SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, "
    "customer.status, customer.test_account, customer.manager FROM customer"
)


# ---------------------------------------------------------------------- shared helpers
def make_client(ctx: Context, args: argparse.Namespace, *, need_customer: bool = True) -> GoogleAdsClient:
    settings = ctx.settings.google
    customer = getattr(args, "customer_id", None) or settings.customer_id
    if need_customer and not customer:
        raise ConfigError(
            "no Google Ads customer id", hint="set [google] customer_id in the config file or pass --customer-id"
        )
    return GoogleAdsClient(
        load_google_credentials(settings, ctx.env),
        customer,
        api_version=settings.api_version,
        api_base_url=settings.api_base_url,
        oauth_token_url=settings.oauth_token_url,
        login_customer_id=getattr(args, "login_customer_id", None) or settings.login_customer_id,
        sleep=ctx.sleep,
        clock=ctx.monotonic,
        allow_test_endpoints=ctx.settings.test_endpoints,
    )


def fmt_cid(customer_id: str | None) -> str:
    cid = str(customer_id or "")
    return f"{cid[:3]}-{cid[3:6]}-{cid[6:]}" if len(cid) == 10 else cid


def customer_info(client: GoogleAdsClient) -> dict[str, Any]:
    if "customer" not in client.cache:
        rows = client.search(CUSTOMER_QUERY)
        client.cache["customer"] = rows[0].get("customer", {}) if rows else {}
    return client.cache["customer"]


def account_today(ctx: Context, client: GoogleAdsClient) -> tuple[date, str | None]:
    tz = customer_info(client).get("timeZone")
    local, known = account_now(tz, ctx.now())
    if tz and not known:
        ctx.out.warn(f"unknown time zone {tz!r}; using UTC dates")
    return local.date(), tz


def try_search(client: GoogleAdsClient, query: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Run a query that is allowed to fail (missing permission, unsupported field)."""
    try:
        return client.search(query), None
    except RateLimited:
        raise
    except ApiError as exc:
        return None, str(exc)


def ctr(clicks: int, impressions: int) -> float | None:
    return clicks / impressions if impressions else None


def cpc(cost_micros: int, clicks: int) -> Decimal | None:
    return from_micros(cost_micros) / clicks if clicks else None


def metric_values(metrics: dict[str, Any]) -> dict[str, Any]:
    impressions = as_int(metrics.get("impressions"))
    clicks = as_int(metrics.get("clicks"))
    cost = as_int(metrics.get("costMicros"))
    return {
        "impressions": impressions,
        "clicks": clicks,
        "cost": from_micros(cost),
        "ctr": ctr(clicks, impressions),
        "cpc": cpc(cost, clicks),
        "conversions": float(metrics.get("conversions") or 0),
    }


def _metric_cells(values: dict[str, Any]) -> list[str]:
    return [
        fmt_int(values["impressions"]),
        fmt_int(values["clicks"]),
        fmt_pct(values["ctr"]),
        fmt_amount(values["cpc"]),
        fmt_amount(values["cost"]),
        f"{values['conversions']:.1f}",
    ]


METRIC_HEADERS = ["impressions", "clicks", "CTR", "CPC", "cost", "conv."]


def _sum_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = {"impressions": 0, "clicks": 0, "costMicros": 0, "conversions": 0.0}
    for row in rows:
        metrics = row.get("metrics") or {}
        total["impressions"] += as_int(metrics.get("impressions"))
        total["clicks"] += as_int(metrics.get("clicks"))
        total["costMicros"] += as_int(metrics.get("costMicros"))
        total["conversions"] += float(metrics.get("conversions") or 0)
    return metric_values(total)


# ---------------------------------------------------------------------- prepared writes
@dataclass
class Prepared:
    """A write that has been read, checked against the guards and turned into one mutate operation."""

    action: str
    target: str
    description: str
    service: str
    operation: dict[str, Any] | None
    expected: Any
    read_back: Callable[[], Any]
    notes: list[str] = field(default_factory=list)
    show: Callable[[Any], str] = str
    noop_reason: str = "already in place, nothing to do"

    @property
    def noop(self) -> bool:
        return self.operation is None


def run_prepared(ctx: Context, client: GoogleAdsClient, prepared: Prepared, *, apply_flag: bool) -> int:
    if prepared.noop:
        ctx.out.line(f"NO-OP    {prepared.description}: {prepared.noop_reason}")
        settle_noop(ctx, "google", prepared.action, prepared.target, prepared.expected)
        ctx.out.emit({"platform": "google", "action": prepared.action, "target": prepared.target, "noop": True})
        return ExitCode.OK
    operation = prepared.operation
    return execute(
        ctx,
        Change(
            platform="google",
            action=prepared.action,
            target=prepared.target,
            description=prepared.description,
            request={"service": prepared.service, "operations": [operation]},
            validate=lambda: client.mutate(prepared.service, [operation], validate_only=True),
            apply=lambda: client.mutate(prepared.service, [operation], validate_only=False),
            read_back=prepared.read_back,
            expected=prepared.expected,
            show=prepared.show,
            notes=prepared.notes,
        ),
        apply_flag=apply_flag,
    )


def prepare_budget(
    ctx: Context, client: GoogleAdsClient, campaign_id: Any, amount: Decimal, *, override: bool, shared_ok: bool
) -> Prepared:
    cid = gaql_id(campaign_id, "campaign id")
    currency = customer_info(client).get("currencyCode")
    rows = client.search(
        "SELECT campaign.id, campaign.name, campaign.status, campaign_budget.resource_name, "
        "campaign_budget.amount_micros, campaign_budget.explicitly_shared, campaign_budget.reference_count "
        f"FROM campaign WHERE campaign.id = {cid}"
    )
    if not rows:
        raise GuardRefused(f"campaign {cid} was not found in account {fmt_cid(client.customer_id)}")
    campaign, budget = rows[0].get("campaign") or {}, rows[0].get("campaignBudget") or {}
    resource = budget.get("resourceName")
    old = as_int(budget.get("amountMicros"))
    if not resource or not old:
        raise GuardRefused(f"campaign {cid} has no daily budget amount that can be set here")
    new = to_micros(amount)

    def show(value: Any) -> str:
        return fmt_amount(from_micros(value), currency) if isinstance(value, int) else str(value)

    def read_back() -> int | None:
        found = client.search(
            "SELECT campaign_budget.amount_micros FROM campaign_budget "
            f"WHERE campaign_budget.resource_name = {gaql_string(resource)}"
        )
        return as_int(found[0]["campaignBudget"].get("amountMicros")) if found else None

    description = f'Campaign {cid} "{campaign.get("name", "")}": daily budget {show(old)} -> {show(new)}'
    if new == old:
        return Prepared("budget.set", resource, description, "campaignBudgets", None, new, read_back, show=show)
    notes: list[str] = []
    references = as_int(budget.get("referenceCount"))
    if budget.get("explicitlyShared") or references > 1:
        if not shared_ok:
            raise GuardRefused(
                f"the budget of campaign {cid} is shared by {references or 'several'} campaigns; "
                "changing it changes all of them",
                hint="pass --shared-ok if that is what you want",
            )
        notes.append(f"shared budget: this changes {references or 'several'} campaigns (--shared-ok)")
    note = check_ceiling(
        increasing=new > old,
        amount=amount,
        ceiling=ctx.settings.google.max_daily_budget,
        setting="[google] max_daily_budget",
        override=override,
        currency=currency,
        what="the daily budget",
    )
    if note:
        notes.append(note)
    operation = {"update": {"resourceName": resource, "amountMicros": str(new)}, "updateMask": "amount_micros"}
    return Prepared("budget.set", resource, description, "campaignBudgets", operation, new, read_back, notes, show)


_LEVELS = {
    # level: (mutate service, JSON key, GAQL resource)
    "campaign": ("campaigns", "campaign", "campaign"),
    "ad_group": ("adGroups", "adGroup", "ad_group"),
    "ad": ("adGroupAds", "adGroupAd", "ad_group_ad"),
}


def prepare_status(
    ctx: Context, client: GoogleAdsClient, level: str, ident: str, want: str, *, override: bool
) -> Prepared:
    customer = client.customer_id
    currency = customer_info(client).get("currencyCode")
    if level == "campaign":
        number = gaql_id(ident, "campaign id")
        label = f"Campaign {number}"
        query = (
            "SELECT campaign.resource_name, campaign.name, campaign.status, campaign_budget.amount_micros "
            f"FROM campaign WHERE campaign.id = {number}"
        )
    elif level == "ad_group":
        number = gaql_id(ident, "ad group id")
        label = f"Ad group {number}"
        query = (
            "SELECT ad_group.resource_name, ad_group.name, ad_group.status, campaign.id, campaign.status "
            f"FROM ad_group WHERE ad_group.id = {number}"
        )
    else:
        group, ad = parse_ad_ref(ident)
        label = f"Ad {group}~{ad}"
        query = (
            "SELECT ad_group_ad.resource_name, ad_group_ad.status, ad_group_ad.ad.name, campaign.id, "
            "campaign.status FROM ad_group_ad WHERE ad_group_ad.resource_name = "
            + gaql_string(f"customers/{customer}/adGroupAds/{group}~{ad}")
        )
    service, key, resource_type = _LEVELS[level]
    rows = client.search(query)
    if not rows:
        raise GuardRefused(f"{label.lower()} was not found in account {fmt_cid(customer)}")
    row = rows[0]
    obj = row.get(key) or {}
    resource = obj["resourceName"]
    current = obj.get("status", "UNKNOWN")
    name = obj.get("name") or (obj.get("ad") or {}).get("name") or ""
    description = f'{label} "{name}": {current} -> {want}'
    action = f"{level.replace('_', '-')}.{'enable' if want == 'ENABLED' else 'pause'}"

    def read_back() -> str | None:
        found = client.search(
            f"SELECT {resource_type}.status FROM {resource_type} "
            f"WHERE {resource_type}.resource_name = {gaql_string(resource)}"
        )
        return (found[0].get(key) or {}).get("status") if found else None

    if current == want:
        return Prepared(action, resource, description, service, None, want, read_back)
    if current == "REMOVED":
        if want == "PAUSED":  # a removed object does not serve: for a pause, the goal is already met
            return Prepared(
                action, resource, description, service, None, "REMOVED", read_back,
                noop_reason="it is REMOVED and does not serve, nothing to pause",
            )  # fmt: skip
        raise GuardRefused(f"{label.lower()} is REMOVED and cannot be changed")
    notes: list[str] = []
    campaign_status = (row.get("campaign") or {}).get("status")
    if level != "campaign" and campaign_status and campaign_status != "ENABLED":
        notes.append(f"the campaign is {campaign_status}: nothing serves until the campaign is enabled too")
    if want == "ENABLED":
        micros = (row.get("campaignBudget") or {}).get("amountMicros")
        campaign_id = (row.get("campaign") or {}).get("id")
        if level != "campaign" and campaign_id:
            # campaign_budget is read from the campaign itself: ad groups and ads do not expose it.
            budget_rows = client.search(
                f"SELECT campaign_budget.amount_micros FROM campaign WHERE campaign.id = {gaql_id(campaign_id)}"
            )
            micros = (budget_rows[0].get("campaignBudget") or {}).get("amountMicros") if budget_rows else None
        note = check_ceiling(
            increasing=True,
            amount=from_micros(micros) if micros else None,
            ceiling=ctx.settings.google.max_daily_budget,
            setting="[google] max_daily_budget",
            override=override,
            currency=currency,
            what="the campaign's daily budget",
        )
        if note:
            notes.append(note)
    operation = {"update": {"resourceName": resource, "status": want}, "updateMask": "status"}
    return Prepared(action, resource, description, service, operation, want, read_back, notes)


# ---------------------------------------------------------------------- read-only commands
def cmd_accounts(ctx: Context, args: argparse.Namespace) -> int:
    client = make_client(ctx, args, need_customer=False)
    ids = client.list_accessible_customers()
    data: dict[str, Any] = {"accessible": ids, "managed": []}
    ctx.out.line("Accounts the OAuth user can access directly:")
    for cid in ids:
        ctx.out.line(f"  {fmt_cid(cid)}")
    if not ids:
        ctx.out.line("  (none)")
    if client.login_customer_id:
        rows = client.search(
            "SELECT customer_client.id, customer_client.descriptive_name, customer_client.manager, "
            "customer_client.level, customer_client.status, customer_client.currency_code, "
            "customer_client.time_zone FROM customer_client",
            customer_id=client.login_customer_id,
        )
        ctx.out.line(f"\nAccounts under the manager account {fmt_cid(client.login_customer_id)}:")
        table = []
        for row in rows:
            c = row.get("customerClient") or {}
            entry = {
                "id": str(c.get("id", "")),
                "name": c.get("descriptiveName", ""),
                "manager": bool(c.get("manager")),
                "level": as_int(c.get("level")),
                "status": c.get("status"),
                "currency": c.get("currencyCode"),
                "time_zone": c.get("timeZone"),
            }
            data["managed"].append(entry)
            table.append(
                [
                    "  " * entry["level"] + fmt_cid(entry["id"]),
                    entry["status"],
                    entry["currency"],
                    entry["time_zone"],
                    "manager" if entry["manager"] else "",
                    entry["name"],
                ]
            )
        ctx.out.table(["id", "status", "currency", "time zone", "", "name"], table)
    ctx.out.emit(data)
    return ExitCode.OK


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    client = make_client(ctx, args)
    info = customer_info(client)
    currency = info.get("currencyCode")
    today, tz = account_today(ctx, client)
    start = today - timedelta(days=29)
    out = ctx.out
    out.line(
        f'Account {fmt_cid(client.customer_id)} "{info.get("descriptiveName", "")}" | {currency} | {tz} | '
        f"{info.get('status')}" + (" | TEST ACCOUNT" if info.get("testAccount") else "")
    )
    if info.get("manager"):
        out.warn("this is a manager (MCC) account: reports and changes need a client account id")

    budget_rows, budget_error = try_search(
        client,
        "SELECT account_budget.id, account_budget.status, account_budget.approved_spending_limit_micros, "
        "account_budget.approved_spending_limit_type, account_budget.amount_served_micros, "
        "account_budget.approved_start_date_time, account_budget.approved_end_date_time FROM account_budget",
    )
    budgets = []
    for row in budget_rows or []:
        b = row.get("accountBudget") or {}
        limit = b.get("approvedSpendingLimitMicros")
        budgets.append(
            {
                "id": str(b.get("id", "")),
                "status": b.get("status"),
                "limit": from_micros(limit) if limit else b.get("approvedSpendingLimitType"),
                "served": from_micros(b.get("amountServedMicros")),
                "start": b.get("approvedStartDateTime"),
                "end": b.get("approvedEndDateTime"),
            }
        )
    out.line("\nAccount budgets (monthly invoicing):")
    if budgets:
        out.table(
            ["id", "status", "limit", "served", "start", "end"],
            [
                [
                    b["id"],
                    b["status"],
                    fmt_amount(b["limit"]) if isinstance(b["limit"], Decimal) else b["limit"],
                    fmt_amount(b["served"]),
                    b["start"],
                    b["end"] or "-",
                ]
                for b in budgets
            ],
            numeric=[2, 3],
        )
    else:
        out.line(
            "  (none visible: normal for accounts on automatic payments)"
            if not budget_error
            else f"  (not readable: {budget_error})"
        )

    campaigns = client.search(
        "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
        "campaign_budget.amount_micros, campaign_budget.explicitly_shared FROM campaign "
        "WHERE campaign.status != 'REMOVED' ORDER BY campaign.id"
    )
    metrics: dict[str, dict[str, Any]] = {}
    for row in client.search(
        "SELECT campaign.id, metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions "
        f"FROM campaign WHERE {between(start, today)} AND campaign.status != 'REMOVED'"
    ):
        metrics[str((row.get("campaign") or {}).get("id"))] = metric_values(row.get("metrics") or {})
    items = []
    for row in campaigns:
        c, b = row.get("campaign") or {}, row.get("campaignBudget") or {}
        m = metrics.get(str(c.get("id")), metric_values({}))
        items.append(
            {
                "id": str(c.get("id", "")),
                "name": c.get("name", ""),
                "status": c.get("status"),
                "type": c.get("advertisingChannelType"),
                "daily_budget": from_micros(b["amountMicros"]) if b.get("amountMicros") else None,
                "shared_budget": bool(b.get("explicitlyShared")),
                "last_30_days": m,
            }
        )
    out.line(f"\nCampaigns (cost: {start} .. {today}, account time zone):")
    if items:
        out.table(
            ["id", "status", "type", "daily budget", "clicks", "cost", "name"],
            [
                [
                    i["id"],
                    i["status"],
                    i["type"],
                    fmt_amount(i["daily_budget"]) + (" (shared)" if i["shared_budget"] else ""),
                    fmt_int(i["last_30_days"]["clicks"]),
                    fmt_amount(i["last_30_days"]["cost"]),
                    i["name"],
                ]
                for i in items
            ],
            numeric=[3, 4, 5],
        )
    else:
        out.line("  (none)")
    out.emit(
        {
            "customer": {
                "id": client.customer_id,
                "name": info.get("descriptiveName"),
                "currency": currency,
                "time_zone": tz,
                "status": info.get("status"),
                "test_account": bool(info.get("testAccount")),
                "manager": bool(info.get("manager")),
            },
            "account_budgets": budgets,
            "account_budgets_error": budget_error,
            "campaigns": items,
            "period": {"from": start, "to": today},
        }
    )
    return ExitCode.OK


def cmd_report(ctx: Context, args: argparse.Namespace) -> int:
    client = make_client(ctx, args)
    currency = customer_info(client).get("currencyCode")
    today, tz = account_today(ctx, client)
    start, end = resolve_range(days=args.days, date_from=args.date_from, date_to=args.date_to, today=today)
    where = between(start, end)
    if args.campaign:
        where += f" AND campaign.id = {gaql_id(args.campaign, 'campaign id')}"
    fields = "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions"
    out = ctx.out
    out.line(f"Google Ads report  {fmt_cid(client.customer_id)}  {start} .. {end}  ({tz}, {currency})")
    rows_out: list[dict[str, Any]] = []
    if args.by == "day":
        resource = "campaign" if args.campaign else "customer"
        rows = client.search(f"SELECT segments.date, {fields} FROM {resource} WHERE {where} ORDER BY segments.date")
        by_day: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_day.setdefault((row.get("segments") or {}).get("date", "?"), []).append(row)
        for day in sorted(by_day):
            rows_out.append({"date": day, **_sum_metrics(by_day[day])})
        out.table(["date", *METRIC_HEADERS], [[r["date"], *_metric_cells(r)] for r in rows_out], numeric=range(1, 7))
        total_rows = rows
    else:
        # Campaigns removed during the period still spent money in it: keep them, so the total matches
        # --by day. Only REMOVED rows without any activity are left out.
        query = f"SELECT campaign.id, campaign.name, campaign.status, {fields} FROM campaign WHERE {where}"
        rows = [
            row
            for row in client.search(query)
            if not (
                (row.get("campaign") or {}).get("status") == "REMOVED"
                and not as_int((row.get("metrics") or {}).get("costMicros"))
                and not as_int((row.get("metrics") or {}).get("impressions"))
            )
        ]
        for row in rows:
            c = row.get("campaign") or {}
            rows_out.append(
                {
                    "campaign_id": str(c.get("id", "")),
                    "name": c.get("name", ""),
                    "status": c.get("status"),
                    **metric_values(row.get("metrics") or {}),
                }
            )
        rows_out.sort(key=lambda r: r["cost"], reverse=True)
        out.table(
            ["campaign", "status", *METRIC_HEADERS, "name"],
            [[r["campaign_id"], r["status"], *_metric_cells(r), r["name"]] for r in rows_out],
            numeric=range(2, 8),
        )
        total_rows = rows
    total = _sum_metrics(total_rows)
    out.line("  total: " + "  ".join(f"{h} {v}" for h, v in zip(METRIC_HEADERS, _metric_cells(total), strict=True)))
    out.emit(
        {
            "customer_id": client.customer_id,
            "currency": currency,
            "time_zone": tz,
            "from": start,
            "to": end,
            "by": args.by,
            "rows": rows_out,
            "total": total,
        }
    )
    return ExitCode.OK


# ---------------------------------------------------------------------- writes
def cmd_budget_set(ctx: Context, args: argparse.Namespace) -> int:
    client = make_client(ctx, args)
    amount = parse_amount(args.amount, "--amount")
    prepared = prepare_budget(
        ctx, client, args.campaign, amount, override=args.override_limit, shared_ok=args.shared_ok
    )
    return run_prepared(ctx, client, prepared, apply_flag=args.apply)


def _level(args: argparse.Namespace) -> tuple[str, str]:
    if args.campaign:
        return "campaign", args.campaign
    if args.ad_group:
        return "ad_group", args.ad_group
    return "ad", args.ad


def cmd_pause(ctx: Context, args: argparse.Namespace) -> int:
    client = make_client(ctx, args)
    level, ident = _level(args)
    prepared = prepare_status(ctx, client, level, ident, "PAUSED", override=False)
    return run_prepared(ctx, client, prepared, apply_flag=args.apply)


def cmd_enable(ctx: Context, args: argparse.Namespace) -> int:
    client = make_client(ctx, args)
    level, ident = _level(args)
    prepared = prepare_status(ctx, client, level, ident, "ENABLED", override=args.override_limit)
    return run_prepared(ctx, client, prepared, apply_flag=args.apply)


CHANNELS = (
    ("youtube_in_stream", "youtubeInStream"),
    ("youtube_in_feed", "youtubeInFeed"),
    ("youtube_shorts", "youtubeShorts"),
    ("discover", "discover"),
    ("gmail", "gmail"),
    ("display", "display"),
)
CHANNEL_PREFIX = "demand_gen_ad_group_settings.channel_controls.selected_channels."


def _fmt_channels(value: Any) -> str:
    if not isinstance(value, dict):
        return "unknown"
    return ", ".join(f"{name.replace('_', '-')} {'on' if value.get(name) else 'off'}" for name, _ in CHANNELS)


def cmd_channel_controls(ctx: Context, args: argparse.Namespace) -> int:
    client = make_client(ctx, args)
    group_id = gaql_id(args.ad_group, "ad group id")
    requested = {name: getattr(args, name) == "on" for name, _ in CHANNELS if getattr(args, name) is not None}
    fields = ", ".join(f"ad_group.{CHANNEL_PREFIX}{name}" for name, _ in CHANNELS)

    def read() -> tuple[dict[str, Any], dict[str, bool] | None]:
        rows = client.search(
            "SELECT ad_group.resource_name, ad_group.name, ad_group.status, campaign.id, "
            f"campaign.advertising_channel_type, {fields} FROM ad_group WHERE ad_group.id = {group_id}"
        )
        if not rows:
            raise GuardRefused(f"ad group {group_id} was not found in account {fmt_cid(client.customer_id)}")
        settings = (rows[0].get("adGroup") or {}).get("demandGenAdGroupSettings") or {}
        selected = (settings.get("channelControls") or {}).get("selectedChannels")
        # The REST API omits false values, so a missing key means "off" once the object exists at all.
        current = {name: bool(selected.get(camel, False)) for name, camel in CHANNELS} if selected else None
        return rows[0], current

    row, current = read()
    group = row.get("adGroup") or {}
    channel_type = (row.get("campaign") or {}).get("advertisingChannelType")
    ctx.out.line(f'Ad group {group_id} "{group.get("name", "")}" ({channel_type}): {_fmt_channels(current)}')
    if not requested:
        ctx.out.emit({"ad_group_id": str(group_id), "campaign_type": channel_type, "channels": current})
        return ExitCode.OK
    if channel_type != "DEMAND_GEN":
        raise GuardRefused(
            f"channel controls apply to Demand Gen campaigns; this ad group is in a {channel_type} campaign"
        )
    if current is None and len(requested) < len(CHANNELS):
        raise GuardRefused(
            "the current channel selection is not readable (the ad group may use all channels)",
            hint="pass all six channel options so the result is fully defined",
        )
    target = dict(current or {})
    target.update(requested)
    if not any(target.values()):
        raise GuardRefused("at least one channel must stay on")
    changed = [
        name for name, _ in CHANNELS if name in requested and (current is None or current[name] != requested[name])
    ]
    expected = {name: target[name] for name, _ in CHANNELS}

    def read_back() -> dict[str, bool] | None:
        return read()[1]

    description = f'Ad group {group_id} "{group.get("name", "")}": ' + (
        ", ".join(
            f"{name.replace('_', '-')} {('on' if current[name] else 'off') if current else '?'} -> "
            f"{'on' if target[name] else 'off'}"
            for name in changed
        )
        or "no change"
    )
    camel = dict(CHANNELS)
    operation = (
        {
            "update": {
                "resourceName": group["resourceName"],
                "demandGenAdGroupSettings": {
                    "channelControls": {"selectedChannels": {camel[name]: target[name] for name in changed}}
                },
            },
            "updateMask": ",".join(CHANNEL_PREFIX + name for name in changed),
        }
        if changed
        else None
    )
    prepared = Prepared(
        "channel-controls.set",
        group["resourceName"],
        description,
        "adGroups",
        operation,
        expected,
        read_back,
        ["leaf field masks: only the channels named above are sent; the others stay as they are"],
        _fmt_channels,
    )
    return run_prepared(ctx, client, prepared, apply_flag=args.apply)


CONVERSION_FIELDS = (
    "conversion_action.resource_name, conversion_action.id, conversion_action.name, conversion_action.type, "
    "conversion_action.category, conversion_action.status, conversion_action.primary_for_goal, "
    "conversion_action.counting_type"
)


def find_conversion(client: GoogleAdsClient, name: str) -> dict[str, Any] | None:
    rows = client.search(
        f"SELECT {CONVERSION_FIELDS}, conversion_action.tag_snippets FROM conversion_action "
        f"WHERE conversion_action.name = {gaql_string(name)} AND conversion_action.status != 'REMOVED'"
    )
    return (rows[0].get("conversionAction") or {}) if rows else None


def send_to_of(action: dict[str, Any]) -> str | None:
    """The ``send_to`` value (``AW-<id>/<label>``) from the action's event snippet."""
    for snippet in action.get("tagSnippets") or []:
        text = f"{snippet.get('eventSnippet') or ''}\n{snippet.get('globalSiteTag') or ''}"
        match = re.search(r"send_to['\"]?\s*:\s*['\"](AW-\d+/[A-Za-z0-9_-]+)['\"]", text)
        if match:
            return match.group(1)
    return None


def cmd_conversion_list(ctx: Context, args: argparse.Namespace) -> int:
    client = make_client(ctx, args)
    rows = client.search(
        f"SELECT {CONVERSION_FIELDS} FROM conversion_action WHERE conversion_action.status != 'REMOVED' "
        "ORDER BY conversion_action.id"
    )
    items = []
    for row in rows:
        a = row.get("conversionAction") or {}
        items.append(
            {
                "id": str(a.get("id", "")),
                "name": a.get("name", ""),
                "type": a.get("type"),
                "category": a.get("category"),
                "status": a.get("status"),
                "primary": bool(a.get("primaryForGoal")),
                "counting": a.get("countingType"),
            }
        )
    ctx.out.table(
        ["id", "status", "type", "category", "goal", "counting", "name"],
        [
            [
                i["id"],
                i["status"],
                i["type"],
                i["category"],
                "primary" if i["primary"] else "secondary",
                i["counting"],
                i["name"],
            ]
            for i in items
        ],
    )
    if not items:
        ctx.out.line("  (no conversion actions)")
    ctx.out.emit({"conversion_actions": items})
    return ExitCode.OK


def cmd_conversion_create(ctx: Context, args: argparse.Namespace) -> int:
    name = args.name.strip()
    if not name or len(name) > 100:
        raise ConfigError("--name must be 1 to 100 characters")
    if not 1 <= args.click_lookback_days <= 90 or not 1 <= args.view_lookback_days <= 30:
        raise ConfigError("--click-lookback-days must be 1-90 and --view-lookback-days 1-30")
    value = parse_amount(args.value, "--value")
    client = make_client(ctx, args)
    out = ctx.out
    existing = find_conversion(client, name)
    if existing:
        send_to = send_to_of(existing)
        out.line(f'NO-OP    conversion action "{name}" already exists: {existing.get("resourceName")}')
        if send_to:
            out.line(f"         send_to for your page: {send_to}")
        out.emit({"noop": True, "resource_name": existing.get("resourceName"), "send_to": send_to})
        return ExitCode.OK
    counting = "MANY_PER_CLICK" if args.counting == "many" else "ONE_PER_CLICK"
    create = {
        "name": name,
        "type": "WEBPAGE",
        "category": args.category,
        "status": "ENABLED",
        "countingType": counting,
        "primaryForGoal": bool(args.primary),
        "valueSettings": {"defaultValue": float(value), "alwaysUseDefaultValue": True},
        "clickThroughLookbackWindowDays": args.click_lookback_days,
        "viewThroughLookbackWindowDays": args.view_lookback_days,
    }
    operation = {"create": create}
    expected = {
        "name": name,
        "type": "WEBPAGE",
        "category": args.category,
        "status": "ENABLED",
        "primaryForGoal": bool(args.primary),
    }

    def show(value: Any) -> str:
        if not isinstance(value, dict):
            return "nothing"
        goal = "primary" if value.get("primaryForGoal") else "secondary"
        return f'"{value.get("name")}" {value.get("type")} {value.get("category")} {value.get("status")} {goal}'

    def read_back() -> dict[str, Any] | None:
        found = find_conversion(client, name)
        if not found:
            return None
        return {key: (bool(found.get(key)) if key == "primaryForGoal" else found.get(key)) for key in expected}

    def on_success() -> dict[str, Any]:
        found = find_conversion(client, name) or {}
        send_to = send_to_of(found)
        ctx.store().update(
            lambda d: d.setdefault("google_conversion_actions", {}).__setitem__(
                f"{client.customer_id}:{name}", found.get("resourceName")
            )
        )
        out.line(f"CREATED  {found.get('resourceName')}")
        if send_to:
            out.line(f"         send_to for snippets/landing-consent-conversion.html: {send_to}")
        return {"resource_name": found.get("resourceName"), "send_to": send_to}

    notes = [
        "PRIMARY action: campaigns that bid on this goal will optimise toward it"
        if args.primary
        else "secondary action: reported in Google Ads, not used for bidding (pass --primary to change)"
    ]
    change = Change(
        platform="google",
        action="conversion-action.create",
        target=f"customers/{client.customer_id}/conversionActions",
        description=(
            f'create WEBPAGE conversion action "{name}" (category {args.category}, {counting}, '
            f"value {value}, click window {args.click_lookback_days} d, view window {args.view_lookback_days} d)"
        ),
        request={"service": "conversionActions", "operations": [operation]},
        validate=lambda: client.mutate("conversionActions", [operation], validate_only=True),
        apply=lambda: client.mutate("conversionActions", [operation], validate_only=False),
        read_back=read_back,
        expected=expected,
        show=show,
        notes=notes,
        on_success=on_success,
    )
    return execute(ctx, change, apply_flag=args.apply)
