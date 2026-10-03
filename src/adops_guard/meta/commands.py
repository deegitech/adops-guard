"""Meta Marketing API commands: whoami, status, report, set status/budget, spend cap, usage, video check."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from adops_guard.changes import Change, execute, settle_noop
from adops_guard.context import Context
from adops_guard.credentials import load_meta_token
from adops_guard.dates import account_now, resolve_range
from adops_guard.errors import ConfigError, ExitCode, GuardRefused
from adops_guard.guards import check_ceiling
from adops_guard.meta.client import MetaClient
from adops_guard.money import fmt_amount, from_minor, meta_offset, parse_amount, to_minor
from adops_guard.output import fmt_int, fmt_pct

ACCOUNT_STATUS = {
    1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW", 8: "PENDING_SETTLEMENT",
    9: "IN_GRACE_PERIOD", 100: "PENDING_CLOSURE", 101: "CLOSED", 201: "ANY_ACTIVE", 202: "ANY_CLOSED",
}  # fmt: skip
ALL_STATUSES = [
    "ACTIVE", "PAUSED", "PENDING_REVIEW", "DISAPPROVED", "PREAPPROVED", "PENDING_BILLING_INFO",
    "CAMPAIGN_PAUSED", "ARCHIVED", "ADSET_PAUSED", "IN_PROCESS", "WITH_ISSUES",
]  # fmt: skip
NO_SPEND_CAP = 922337203685478  # Meta's documented value for "remove the campaign spend cap"
NO_ACTIVE_ADSETS = "no active ad sets"
ACCOUNT_FIELDS = (
    "id,name,account_status,disable_reason,currency,timezone_name,amount_spent,spend_cap,"
    "min_daily_budget,min_campaign_group_spend_cap"
)
PROBE_FIELDS = "id,name,status,effective_status,account_id,campaign_id,adset_id,daily_budget,lifetime_budget,spend_cap"
IG_OBSERVED_NOTE = (
    "observed (Oct 2026): videos of about 17 to 19 s came back is_instagram_eligible=false, and a 14.8 s cut of "
    "the same video came back true. The exact limit was not tested: videos of --max-seconds (15) or longer are "
    "flagged. Meta does not document this; it may change."
)


# ---------------------------------------------------------------------- helpers
def normalize_account(value: str | None) -> str:
    match = re.fullmatch(r"(?:act_)?(\d{5,25})", (value or "").strip())
    if not match:
        raise ConfigError(f"ad account ids look like act_123456789012345 (got {value!r})")
    return f"act_{match.group(1)}"


def object_id(value: str, what: str = "object id") -> str:
    text = str(value).strip()
    if not re.fullmatch(r"\d{5,25}", text):
        raise ConfigError(f"{what} must be digits only (got {value!r})")
    return text


def account_of(ctx: Context, args: argparse.Namespace, *, required: bool = True) -> str | None:
    value = getattr(args, "ad_account", None) or ctx.settings.meta.ad_account_id
    if not value:
        if required:
            raise ConfigError(
                "no Meta ad account configured",
                hint="set [meta] ad_account_id in the config file or pass --ad-account; writes are limited to it",
            )
        return None
    return normalize_account(value)


def make_client(ctx: Context) -> MetaClient:
    settings = ctx.settings.meta
    return MetaClient(
        load_meta_token(settings, ctx.env, ctx.runner),
        api_version=settings.api_version,
        base_url=settings.graph_base_url,
        usage_stop=settings.usage_stop_percent,
        sleep=ctx.sleep,
        allow_test_endpoints=ctx.settings.test_endpoints,
    )


def minor(value: Any) -> int | None:
    """A positive minor-unit amount, or None for missing / zero."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def account_info(client: MetaClient, account: str) -> dict[str, Any]:
    return client.get_fields(account, ACCOUNT_FIELDS)


def read_object(client: MetaClient, oid: str, expected_type: str | None = None) -> dict[str, Any]:
    """Read a campaign, ad set or ad, and work out which it is from the fields it has."""
    obj = client.get_fields(oid, PROBE_FIELDS)
    kind = "ad" if "adset_id" in obj else "adset" if "campaign_id" in obj else "campaign"
    if expected_type and kind != expected_type:
        raise GuardRefused(f"{oid} is a {kind}, not a {expected_type}")
    obj["_type"] = kind
    return obj


def ensure_in_account(obj: dict[str, Any], account: str) -> None:
    if str(obj.get("account_id") or "") != account.removeprefix("act_"):
        raise GuardRefused(
            f"{obj.get('_type', 'object')} {obj.get('id')} belongs to ad account act_{obj.get('account_id')}, "
            f"not the configured {account}; nothing was changed"
        )


def effective_budget(client: MetaClient, obj: dict[str, Any]) -> tuple[int | None, int | None, str]:
    """(daily, lifetime, where) in minor units: the budget that decides spend if ``obj`` goes live."""
    kind = obj["_type"]
    own = minor(obj.get("daily_budget")), minor(obj.get("lifetime_budget"))
    if kind == "campaign":
        if any(own):
            return own[0], own[1], "campaign budget"
        sets = client.get_all(
            f"{obj['id']}/adsets",
            {"fields": "id,status,daily_budget,lifetime_budget", "effective_status": ALL_STATUSES},
        )
        live = [s for s in sets if s.get("status") == "ACTIVE"]
        if not live:
            return None, None, NO_ACTIVE_ADSETS
        daily = sum(minor(s.get("daily_budget")) or 0 for s in live)
        lifetime = sum(minor(s.get("lifetime_budget")) or 0 for s in live)
        return daily or None, lifetime or None, f"sum of {len(live)} active ad set(s)"
    adset_id = obj["id"] if kind == "adset" else obj.get("adset_id")
    if kind == "ad":
        adset = client.get_fields(str(adset_id), "id,daily_budget,lifetime_budget")
        own = minor(adset.get("daily_budget")), minor(adset.get("lifetime_budget"))
    if any(own):
        return own[0], own[1], f"ad set {adset_id}"
    campaign = client.get_fields(str(obj.get("campaign_id")), "id,daily_budget,lifetime_budget")
    return minor(campaign.get("daily_budget")), minor(campaign.get("lifetime_budget")), "campaign budget"


def _show_minor(offset: int, currency: str | None) -> Callable[[Any], str]:
    def show(value: Any) -> str:
        return "none" if value is None else fmt_amount(from_minor(value, offset), currency)

    return show


# ---------------------------------------------------------------------- read-only commands
def cmd_whoami(ctx: Context, args: argparse.Namespace) -> int:
    client = make_client(ctx)
    me = client.get("me", {"fields": "id,name"})
    perms = client.get("me/permissions").get("data") or []
    granted = [p.get("permission") for p in perms if p.get("status") == "granted"]
    other = [f"{p.get('permission')} ({p.get('status')})" for p in perms if p.get("status") != "granted"]
    out = ctx.out
    out.line(f"Token user: {me.get('name')} ({me.get('id')}) | Graph API {client.version}")
    out.line("Granted: " + (", ".join(granted) or "none"))
    if other:
        out.line("Not granted: " + ", ".join(other))
    for needed in ("ads_read", "ads_management"):
        if needed not in granted:
            out.warn(f"{needed} is not granted: some commands will fail")
    data: dict[str, Any] = {"user": me, "granted": granted, "not_granted": other}
    account = account_of(ctx, args, required=False)
    if account:
        info = account_info(client, account)
        status = ACCOUNT_STATUS.get(int(info.get("account_status") or 0), str(info.get("account_status")))
        name, currency, tz = info.get("name"), info.get("currency"), info.get("timezone_name")
        out.line(f'Ad account {account} "{name}" | {status} | {currency} | {tz}')
        data["account"] = info
    out.emit(data)
    return ExitCode.OK


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    account = account_of(ctx, args)
    assert account is not None
    client = make_client(ctx)
    info = account_info(client, account)
    currency = info.get("currency")
    offset = meta_offset(currency, ctx.settings.meta.currency_offset)
    show = _show_minor(offset, currency)
    out = ctx.out
    status = ACCOUNT_STATUS.get(int(info.get("account_status") or 0), str(info.get("account_status")))
    out.line(f'Ad account {account} "{info.get("name")}" | {status} | {currency} | {info.get("timezone_name")}')
    cap = minor(info.get("spend_cap"))
    out.line(
        f"  spent {show(minor(info.get('amount_spent')) or 0)} | account spending limit {show(cap) if cap else 'none'}"
        f" | minimum daily budget {show(minor(info.get('min_daily_budget')))}"
    )
    campaigns = client.get_all(
        f"{account}/campaigns",
        {
            "fields": "id,name,objective,status,effective_status,daily_budget,lifetime_budget,spend_cap",
            "effective_status": ALL_STATUSES,
        },
    )

    def budget_text(item: dict[str, Any]) -> str:
        if minor(item.get("daily_budget")):
            return f"{show(minor(item.get('daily_budget')))}/day"
        if minor(item.get("lifetime_budget")):
            return f"{show(minor(item.get('lifetime_budget')))} lifetime"
        return "-"

    def cap_text(item: dict[str, Any]) -> str:
        value = minor(item.get("spend_cap"))
        return "-" if value is None or value >= NO_SPEND_CAP else show(value)

    out.line("\nCampaigns:")
    out.table(
        ["id", "status", "effective", "objective", "budget", "spend cap", "name"],
        [
            [
                c.get("id"),
                c.get("status"),
                c.get("effective_status"),
                c.get("objective"),
                budget_text(c),
                cap_text(c),
                c.get("name"),
            ]
            for c in campaigns
        ],
    )
    data: dict[str, Any] = {"account": info, "campaigns": campaigns}
    if args.campaign:
        cid = object_id(args.campaign, "campaign id")
        sets = client.get_all(
            f"{cid}/adsets",
            {
                "fields": "id,name,status,effective_status,daily_budget,lifetime_budget,optimization_goal",
                "effective_status": ALL_STATUSES,
            },
        )
        ads = client.get_all(
            f"{cid}/ads", {"fields": "id,name,status,effective_status,adset_id", "effective_status": ALL_STATUSES}
        )
        out.line(f"\nAd sets in campaign {cid}:")
        out.table(
            ["id", "status", "effective", "goal", "budget", "name"],
            [
                [
                    s.get("id"),
                    s.get("status"),
                    s.get("effective_status"),
                    s.get("optimization_goal"),
                    budget_text(s),
                    s.get("name"),
                ]
                for s in sets
            ],
        )
        out.line(f"\nAds in campaign {cid}:")
        out.table(
            ["id", "status", "effective", "ad set", "name"],
            [[a.get("id"), a.get("status"), a.get("effective_status"), a.get("adset_id"), a.get("name")] for a in ads],
        )
        data.update(adsets=sets, ads=ads)
    out.emit(data)
    return ExitCode.OK


_LEVEL_FIELDS = {
    "account": [],
    "campaign": ["campaign_id", "campaign_name"],
    "adset": ["adset_id", "adset_name"],
    "ad": ["ad_id", "ad_name"],
}


def cmd_report(ctx: Context, args: argparse.Namespace) -> int:
    account = account_of(ctx, args)
    assert account is not None
    client = make_client(ctx)
    info = account_info(client, account)
    currency, tz = info.get("currency"), info.get("timezone_name")
    local, _ = account_now(tz, ctx.now())
    start, end = resolve_range(days=args.days, date_from=args.date_from, date_to=args.date_to, today=local.date())
    ident = _LEVEL_FIELDS[args.level]
    fields = ["date_start", "date_stop", *ident, "spend", "impressions", "reach", "clicks", "inline_link_clicks"]
    params: dict[str, Any] = {
        "level": args.level,
        "fields": ",".join(fields),
        "time_range": {"since": start.isoformat(), "until": end.isoformat()},
    }
    if args.by_day:
        params["time_increment"] = 1
    rows = client.get_all(f"{account}/insights", params)
    items = []
    for row in rows:
        spend = Decimal(str(row.get("spend") or "0"))
        impressions, clicks = int(row.get("impressions") or 0), int(row.get("clicks") or 0)
        items.append(
            {
                "date": row.get("date_start") if args.by_day else f"{row.get('date_start')}..{row.get('date_stop')}",
                "id": row.get(ident[0]) if ident else account,
                "name": row.get(ident[1]) if ident else info.get("name"),
                "impressions": impressions,
                "reach": int(row.get("reach") or 0),
                "clicks": clicks,
                "link_clicks": int(row.get("inline_link_clicks") or 0),
                "ctr": clicks / impressions if impressions else None,
                "cpc": spend / clicks if clicks else None,
                "spend": spend,
            }
        )
    out = ctx.out
    out.line(f"Meta report  {account}  {start} .. {end}  ({tz}, {currency}, level {args.level})")
    out.table(
        ["date", "id", "impressions", "reach", "clicks", "link clicks", "CTR", "CPC", "spend", "name"],
        [
            [
                i["date"],
                i["id"],
                fmt_int(i["impressions"]),
                fmt_int(i["reach"]),
                fmt_int(i["clicks"]),
                fmt_int(i["link_clicks"]),
                fmt_pct(i["ctr"]),
                fmt_amount(i["cpc"]),
                fmt_amount(i["spend"]),
                (i["name"] or "")[:50],
            ]
            for i in items
        ],
        numeric=range(2, 9),
    )  # fmt: skip
    spend = sum((i["spend"] for i in items), Decimal(0))
    impressions = sum(i["impressions"] for i in items)
    clicks = sum(i["clicks"] for i in items)
    out.line(
        f"  total: spend {fmt_amount(spend, currency)} | impressions {fmt_int(impressions)} | clicks {fmt_int(clicks)}"
        f" | CTR {fmt_pct(clicks / impressions if impressions else None)} (reach is not additive across rows)"
    )
    out.emit(
        {
            "account": account,
            "currency": currency,
            "time_zone": tz,
            "from": start,
            "to": end,
            "level": args.level,
            "rows": items,
            "total": {"spend": spend, "impressions": impressions, "clicks": clicks},
        }
    )
    return ExitCode.OK


def cmd_usage(ctx: Context, args: argparse.Namespace) -> int:
    client = make_client(ctx)
    target = account_of(ctx, args, required=False) or "me"
    client.get(target, {"fields": "id"})
    percent = client.usage_percent()
    out = ctx.out
    out.line(f"Highest usage: {percent:.0f}% (this tool stops at {client.usage_stop}%)")
    for name, value in client.usage.items():
        out.line(f"  {name}: {value}")
    wait = client.regain_minutes()
    if wait:
        out.line(f"  Meta estimates {wait} min until access is fully back")
    out.emit({"percent": percent, "stop_at": client.usage_stop, "headers": client.usage, "regain_minutes": wait})
    return ExitCode.OK


def ffprobe_duration(runner: Any, path: str) -> tuple[float | None, str | None]:
    # "file:" + an absolute path: a name starting with "-" is never read as an option, nor "x:..." as a protocol.
    target = "file:" + os.path.abspath(path)
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", target]
    try:
        result = runner(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        return None, "ffprobe not found (install FFmpeg to check local files)"
    except subprocess.TimeoutExpired:
        return None, "ffprobe timed out"
    try:
        return float((result.stdout or "").strip()), None
    except ValueError:
        return None, "ffprobe could not read the duration"


def cmd_video_check(ctx: Context, args: argparse.Namespace) -> int:
    if not args.video_ids and not args.file:
        raise ConfigError("give one or more VIDEO_ID values and/or --file PATH")
    limit = float(args.max_seconds)
    out = ctx.out
    results: list[dict[str, Any]] = []
    failed = False
    if args.video_ids:
        client = make_client(ctx)
        for raw in args.video_ids:
            vid = object_id(raw, "video id")
            info = client.get_fields(vid, "id,title,length,status,is_instagram_eligible")
            eligible = (
                info.get("is_instagram_eligible") if "is_instagram_eligible" not in info.get("_dropped", []) else None
            )
            length = float(info["length"]) if info.get("length") is not None else None
            state = (
                (info.get("status") or {}).get("video_status")
                if isinstance(info.get("status"), dict)
                else info.get("status")
            )
            verdict = "eligible" if eligible is True else "NOT eligible" if eligible is False else "unknown"
            failed |= eligible is False
            results.append({"video_id": vid, "title": info.get("title"), "length": length, "status": state,
                            "is_instagram_eligible": eligible, "verdict": verdict})  # fmt: skip
            length_text = f"{length:.2f} s" if length is not None else "length ?"
            warn = " (at or over the observed limit)" if length is not None and length >= limit else ""
            title = info.get("title") or ""
            out.line(f"  video {vid}: {verdict} for Instagram placements | {length_text}{warn} | {state} | {title}")
    for path in args.file or []:
        if not Path(path).is_file():
            raise ConfigError(f"file not found: {path}")
        duration, problem = ffprobe_duration(ctx.runner, path)
        over = duration is not None and duration >= limit
        failed |= over
        verdict = "unknown" if duration is None else ("LIKELY NOT eligible" if over else "ok by length")
        results.append({"file": path, "duration": duration, "verdict": verdict, "problem": problem})
        detail = f"{duration:.2f} s" if duration is not None else problem
        out.line(f"  file {path}: {verdict} ({detail}; limit {limit:g} s)")
    out.line(f"  note: {IG_OBSERVED_NOTE}")
    out.emit({"max_seconds": limit, "results": results, "note": IG_OBSERVED_NOTE})
    return ExitCode.REFUSED if failed else ExitCode.OK


# ---------------------------------------------------------------------- writes
def cmd_set_status(ctx: Context, args: argparse.Namespace) -> int:
    account = account_of(ctx, args)
    assert account is not None
    client = make_client(ctx)
    oid = object_id(args.object_id)
    obj = read_object(client, oid, args.type)
    ensure_in_account(obj, account)
    kind, want, current = obj["_type"], args.status, obj.get("status")
    action = f"{kind}.status"
    description = f'{kind} {oid} "{obj.get("name", "")}": {current} -> {want}'

    def read_back() -> Any:
        return client.get(oid, {"fields": "status"}).get("status")

    if current == want:
        ctx.out.line(f"NO-OP    {description}: already {want}, nothing to do")
        settle_noop(ctx, "meta", action, oid, want)
        ctx.out.emit({"platform": "meta", "target": oid, "noop": True})
        return ExitCode.OK
    notes: list[str] = []
    if want == "ACTIVE":
        info = account_info(client, account)
        currency = info.get("currency")
        offset = meta_offset(currency, ctx.settings.meta.currency_offset)
        daily, lifetime, where = effective_budget(client, obj)
        settings = ctx.settings.meta
        checks: list[tuple[Decimal | None, Decimal | None, str, str]] = []
        if where == NO_ACTIVE_ADSETS:
            notes.append(
                "no ad set in this campaign is active: nothing spends until one is; "
                "activating an ad set with adops-guard checks its budget"
            )
        if daily is not None:
            checks.append((from_minor(daily, offset), settings.max_daily_budget, "daily", "the daily budget"))
        if lifetime is not None:
            checks.append(
                (from_minor(lifetime, offset), settings.max_lifetime_budget, "lifetime", "the lifetime budget")
            )
        if not checks and where != NO_ACTIVE_ADSETS:  # no budget found: refused unless --override-limit
            checks.append((None, settings.max_daily_budget, "daily", "the budget"))
        for amount, ceiling, budget_kind, what in checks:
            note = check_ceiling(
                increasing=True,
                amount=amount,
                ceiling=ceiling,
                setting=f"[meta] max_{budget_kind}_budget",
                override=args.override_limit,
                currency=currency,
                what=f"{what} ({where})",
            )
            if note:
                notes.append(note)
        notes.append(
            "a daily budget is not a hard cap (Meta may spend more on some days); use a spend cap for a hard limit"
        )
    change = Change(
        platform="meta",
        action=action,
        target=oid,
        description=description,
        request={"status": want},
        validate=lambda: client.post(oid, {"status": want}, validate_only=True),
        apply=lambda: client.post(oid, {"status": want}),
        read_back=read_back,
        expected=want,
        notes=notes,
        validate_in_dry_run=args.validate,
    )
    return execute(ctx, change, apply_flag=args.apply)


def cmd_set_budget(ctx: Context, args: argparse.Namespace) -> int:
    account = account_of(ctx, args)
    assert account is not None
    kind = "daily" if args.daily is not None else "lifetime"
    amount = parse_amount(args.daily if args.daily is not None else args.lifetime, f"--{kind}")
    client = make_client(ctx)
    oid = object_id(args.object_id)
    obj = read_object(client, oid, args.type)
    ensure_in_account(obj, account)
    if obj["_type"] == "ad":
        raise GuardRefused("ads have no budget; set it on the ad set or the campaign")
    param = f"{kind}_budget"
    current = minor(obj.get(param))
    if current is None:
        raise GuardRefused(
            f"{obj['_type']} {oid} has no {kind} budget",
            hint="it may use the other budget type, or the budget lives on the campaign / the ad sets",
        )
    info = account_info(client, account)
    currency = info.get("currency")
    offset = meta_offset(currency, ctx.settings.meta.currency_offset)
    new = to_minor(amount, offset)
    show = _show_minor(offset, currency)
    floor = minor(info.get("min_daily_budget"))
    if kind == "daily" and floor and new < floor:
        raise GuardRefused(f"{show(new)} is below this account's minimum daily budget {show(floor)}")
    description = f'{obj["_type"]} {oid} "{obj.get("name", "")}": {kind} budget {show(current)} -> {show(new)}'

    def read_back() -> int | None:
        return minor(client.get(oid, {"fields": param}).get(param))

    action = f"{obj['_type']}.{param}"
    if new == current:
        ctx.out.line(f"NO-OP    {description}: already set, nothing to do")
        settle_noop(ctx, "meta", action, oid, new)
        ctx.out.emit({"platform": "meta", "target": oid, "noop": True})
        return ExitCode.OK
    settings = ctx.settings.meta
    note = check_ceiling(
        increasing=new > current,
        amount=amount,
        ceiling=settings.max_daily_budget if kind == "daily" else settings.max_lifetime_budget,
        setting=f"[meta] max_{kind}_budget",
        override=args.override_limit,
        currency=currency,
        what=f"the {kind} budget",
    )
    change = Change(
        platform="meta",
        action=action,
        target=oid,
        description=description,
        request={param: new},
        validate=lambda: client.post(oid, {param: new}, validate_only=True),
        apply=lambda: client.post(oid, {param: new}),
        read_back=read_back,
        expected=new,
        show=show,
        notes=[note] if note else [],
        validate_in_dry_run=args.validate,
    )
    return execute(ctx, change, apply_flag=args.apply)


def cmd_spend_cap(ctx: Context, args: argparse.Namespace) -> int:
    account = account_of(ctx, args)
    assert account is not None
    client = make_client(ctx)
    oid = object_id(args.campaign_id, "campaign id")
    obj = read_object(client, oid, "campaign")
    ensure_in_account(obj, account)
    info = account_info(client, account)
    currency = info.get("currency")
    offset = meta_offset(currency, ctx.settings.meta.currency_offset)
    show = _show_minor(offset, currency)
    current = minor(obj.get("spend_cap"))
    if current is not None and current >= NO_SPEND_CAP:
        current = None
    notes: list[str] = []
    if args.remove:
        new: int | None = None
        sent = NO_SPEND_CAP
        if current is not None and not args.override_limit:
            raise GuardRefused(
                f"removing the spend cap of campaign {oid} removes a hard spending limit",
                hint="pass --override-limit if you really want no cap",
            )
        if current is not None:
            notes.append("--override-limit: the campaign will have no spend cap")
    else:
        amount = parse_amount(args.amount, "--amount")
        new = sent = to_minor(amount, offset)
        floor = minor(info.get("min_campaign_group_spend_cap"))
        if floor and new < floor:
            raise GuardRefused(
                f"{show(new)} is below Meta's minimum campaign spend cap for this account ({show(floor)})"
            )
        note = check_ceiling(
            increasing=current is not None and new > current,  # adding a cap where there was none lowers the limit
            amount=amount,
            ceiling=ctx.settings.meta.max_spend_cap,
            setting="[meta] max_spend_cap",
            override=args.override_limit,
            currency=currency,
            what="the spend cap",
        )
        if note:
            notes.append(note)
    description = f'campaign {oid} "{obj.get("name", "")}": spend cap {show(current)} -> {show(new)}'
    if new == current:
        ctx.out.line(f"NO-OP    {description}: already set, nothing to do")
        settle_noop(ctx, "meta", "campaign.spend_cap", oid, new)
        ctx.out.emit({"platform": "meta", "target": oid, "noop": True})
        return ExitCode.OK

    def read_back() -> int | None:
        value = minor(client.get(oid, {"fields": "spend_cap"}).get("spend_cap"))
        return None if value is None or value >= NO_SPEND_CAP else value

    change = Change(
        platform="meta",
        action="campaign.spend_cap",
        target=oid,
        description=description,
        request={"spend_cap": sent},
        validate=lambda: client.post(oid, {"spend_cap": sent}, validate_only=True),
        apply=lambda: client.post(oid, {"spend_cap": sent}),
        read_back=read_back,
        expected=new,
        show=show,
        notes=notes,
        validate_in_dry_run=args.validate,
    )
    return execute(ctx, change, apply_flag=args.apply)
