"""Click-quality audit for YouTube-heavy campaigns. A heuristic, not a verdict.

It pulls breakdowns that Google Ads already has (channel or ad format,
placements, hour of day, age, day) and checks four signs that clicks may be
accidental or low-intent:

1. **in-stream CTR far above Shorts / in-feed**: people tapping a skippable
   in-stream ad far more often than the same creative elsewhere;
2. **clicks from TV-drama or kids channels**: long full-episode videos and
   children's content, matched by crude name keywords;
3. **night-time share**: many clicks at night with a CTR above the daytime CTR;
4. **CTR rising while CPC falls**: the system finding cheaper, clickier
   inventory over time.

None of these proves anything on its own. They are reasons to compare
conversions (installs, sign-ups) per channel before spending more. Each
breakdown is fetched separately; if the API rejects one (field not available
in your API version, or not for this campaign type) the audit says so and the
related signal is marked ``n/a``.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from adops_guard.config import AuditSettings
from adops_guard.context import Context
from adops_guard.dates import resolve_range
from adops_guard.errors import ExitCode
from adops_guard.google.client import GoogleAdsClient
from adops_guard.google.commands import account_today, customer_info, fmt_cid, make_client, try_search
from adops_guard.google.gaql import as_int, between, gaql_id
from adops_guard.money import from_micros
from adops_guard.output import fmt_int, fmt_pct, fmt_ratio

KIDS_WORDS = (
    "kids", "kid", "children", "child", "toddler", "toddlers", "baby", "babies", "nursery", "rhymes",
    "cartoon", "cartoons", "preschool", "toys", "infantil", "infantiles", "niños", "crianças", "desenho",
    "desenhos", "dibujos animados",
)  # fmt: skip
DRAMA_WORDS = (
    "episode", "episodes", "full episode", "ep", "season", "drama", "dramas", "series", "telenovela",
    "novela", "novelas", "capítulo", "capitulo", "episodio", "episódio", "temporada",
)  # fmt: skip
METRICS = "metrics.impressions, metrics.clicks, metrics.cost_micros"
CHANNEL_QUERIES = (
    ("format", "segments.ad_network_type, segments.ad_format_type"),
    ("format", "segments.ad_format_type"),
    ("network", "segments.ad_network_type"),
)
_QUIET_FORMATS = {"", "OTHER", "UNSEGMENTED", "UNKNOWN", "UNSPECIFIED"}


def _matcher(words: tuple[str, ...]) -> re.Pattern[str]:
    ordered = sorted({w.lower() for w in words if w}, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in ordered) + r")(?!\w)", re.IGNORECASE)


@dataclass
class Totals:
    impressions: int = 0
    clicks: int = 0
    cost_micros: int = 0

    def add(self, metrics: dict[str, Any]) -> None:
        self.impressions += as_int(metrics.get("impressions"))
        self.clicks += as_int(metrics.get("clicks"))
        self.cost_micros += as_int(metrics.get("costMicros"))

    def merge(self, other: Totals) -> None:
        self.impressions += other.impressions
        self.clicks += other.clicks
        self.cost_micros += other.cost_micros

    @property
    def ctr(self) -> float | None:
        return self.clicks / self.impressions if self.impressions else None

    @property
    def cpc(self) -> float | None:
        return self.cost_micros / 1_000_000 / self.clicks if self.clicks else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "impressions": self.impressions,
            "clicks": self.clicks,
            "cost": from_micros(self.cost_micros),
            "ctr": self.ctr,
            "cpc": self.cpc,
        }


def total_of(items: list[Totals]) -> Totals:
    total = Totals()
    for item in items:
        total.merge(item)
    return total


@dataclass
class Breakdown:
    rows: dict[str, Totals] = field(default_factory=dict)
    source: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.source is not None

    def total(self) -> Totals:
        return total_of(list(self.rows.values()))


@dataclass
class Placement:
    kind: str  # "channel" or "video"
    name: str
    placement: str
    placement_type: str
    url: str
    totals: Totals
    flags: list[str] = field(default_factory=list)


@dataclass
class AuditData:
    start: date
    end: date
    campaign_id: int | None
    currency: str | None = None
    time_zone: str | None = None
    channels: Breakdown = field(default_factory=Breakdown)
    channel_mode: str | None = None
    placements: list[Placement] = field(default_factory=list)
    placement_errors: list[str] = field(default_factory=list)
    placements_available: bool = False
    hours: Breakdown = field(default_factory=Breakdown)
    ages: Breakdown = field(default_factory=Breakdown)
    days: Breakdown = field(default_factory=Breakdown)


@dataclass
class Signal:
    key: str
    title: str
    status: str  # "fired", "clear" or "n/a"
    detail: str
    advice: str = ""
    core: bool = True


def channel_label(network: str | None, ad_format: str | None) -> str:
    fmt = (ad_format or "").upper()
    if fmt.startswith("INSTREAM") or fmt == "BUMPER":
        return "youtube in-stream"
    if fmt.startswith("INFEED") or fmt == "IN_FEED":
        return "youtube in-feed"
    if fmt.startswith("SHORTS"):
        return "youtube shorts"
    net = (network or "").lower().replace("_", " ")
    extra = "" if fmt in _QUIET_FORMATS else fmt.lower().replace("_", " ")
    return " ".join(part for part in (net, extra) if part) or "unknown"


# ---------------------------------------------------------------------- fetch
def fetch(
    client: GoogleAdsClient, settings: AuditSettings, campaign_id: int | None, start: date, end: date
) -> AuditData:
    info = customer_info(client)
    data = AuditData(start, end, campaign_id, info.get("currencyCode"), info.get("timeZone"))
    where = between(start, end) + (f" AND campaign.id = {campaign_id}" if campaign_id else "")

    for mode, fields in CHANNEL_QUERIES:
        rows, error = try_search(client, f"SELECT {fields}, {METRICS} FROM campaign WHERE {where}")
        if rows is None:
            data.channels.errors.append(f"{fields}: {error}")
            continue
        data.channels.source, data.channel_mode = fields, mode
        for row in rows:
            seg = row.get("segments") or {}
            label = channel_label(seg.get("adNetworkType"), seg.get("adFormatType"))
            data.channels.rows.setdefault(label, Totals()).add(row.get("metrics") or {})
        break

    kids = _matcher(KIDS_WORDS + settings.extra_kids_keywords)
    drama = _matcher(DRAMA_WORDS + settings.extra_drama_keywords)
    views = (
        ("channel", "group_placement_view", "groupPlacementView",
         "group_placement_view.placement_type, group_placement_view.display_name, "
         "group_placement_view.placement, group_placement_view.target_url"),
        ("video", "detail_placement_view", "detailPlacementView",
         "detail_placement_view.placement_type, detail_placement_view.display_name, "
         "detail_placement_view.placement, detail_placement_view.target_url"),
    )  # fmt: skip
    for kind, view, key, fields in views:
        rows, error = try_search(
            client, f"SELECT {fields}, {METRICS} FROM {view} WHERE {where} ORDER BY metrics.clicks DESC LIMIT 1000"
        )
        if rows is None:
            data.placement_errors.append(f"{view}: {error}")
            continue
        data.placements_available = True
        merged: dict[str, Placement] = {}
        for row in rows:
            p = row.get(key) or {}
            ident = p.get("placement") or p.get("targetUrl") or p.get("displayName") or "?"
            item = merged.get(ident)
            if item is None:
                name = p.get("displayName") or ident
                flags = (["kids"] if kids.search(name) else []) + (["tv-drama"] if drama.search(name) else [])
                item = merged[ident] = Placement(
                    kind, name, ident, p.get("placementType") or "", p.get("targetUrl") or "", Totals(), flags
                )
            item.totals.add(row.get("metrics") or {})
        data.placements.extend(sorted(merged.values(), key=lambda x: x.totals.clicks, reverse=True))

    simple = (
        ("hours", "segments.hour", "campaign", lambda r: str(as_int((r.get("segments") or {}).get("hour")))),
        ("ages", "ad_group_criterion.age_range.type", "age_range_view",
         lambda r: ((r.get("adGroupCriterion") or {}).get("ageRange") or {}).get("type") or "UNKNOWN"),
        ("days", "segments.date", "campaign", lambda r: (r.get("segments") or {}).get("date") or "?"),
    )  # fmt: skip
    for attr, field_name, resource, key_of in simple:
        breakdown: Breakdown = getattr(data, attr)
        rows, error = try_search(client, f"SELECT {field_name}, {METRICS} FROM {resource} WHERE {where}")
        if rows is None:
            breakdown.errors.append(f"{field_name}: {error}")
            continue
        breakdown.source = field_name
        for row in rows:
            breakdown.rows.setdefault(key_of(row), Totals()).add(row.get("metrics") or {})
    return data


# ---------------------------------------------------------------------- analysis
def _in_window(hour: int, window: tuple[int, int]) -> bool:
    start, end = window
    return start <= hour < end if start < end else hour >= start or hour < end


def _instream(data: AuditData, s: AuditSettings) -> Signal:
    key, title = "instream-ctr", "In-stream CTR far above Shorts / in-feed"
    if data.channel_mode != "format":
        why = "; ".join(data.channels.errors[-1:]) or "no ad-format split for this account or API version"
        return Signal(key, title, "n/a", f"no in-stream / in-feed / Shorts split available ({why})")
    rows = data.channels.rows
    instream = total_of([t for label, t in rows.items() if label.endswith("in-stream")])
    others = total_of([t for label, t in rows.items() if label.endswith(("in-feed", "shorts"))])
    total = data.channels.total()
    if instream.clicks < s.min_clicks or others.impressions < s.min_impressions:
        return Signal(
            key, title, "n/a",
            f"not enough data (in-stream clicks {fmt_int(instream.clicks)}, need {s.min_clicks}; "
            f"in-feed + Shorts impressions {fmt_int(others.impressions)}, need {s.min_impressions})",
        )  # fmt: skip
    ratio = (instream.ctr or 0) / others.ctr if others.ctr else float("inf")
    share = instream.clicks / total.clicks if total.clicks else 0.0
    detail = (
        f"in-stream CTR {fmt_pct(instream.ctr)} vs {fmt_pct(others.ctr)} on in-feed + Shorts "
        f"({fmt_ratio(ratio)}; threshold {s.instream_ctr_ratio:.1f}x); in-stream has {fmt_pct(share, 1)} of all clicks"
    )
    advice = (
        "turn YouTube in-stream off for the affected ad groups and judge by conversions, not clicks: "
        "adops-guard google channel-controls --ad-group <ID> --youtube-in-stream off"
    )
    return Signal(key, title, "fired" if ratio >= s.instream_ctr_ratio else "clear", detail, advice)


def _placements(data: AuditData, s: AuditSettings) -> Signal:
    key, title = "kids-tv-placements", "Clicks from TV-drama or kids channels"
    if not data.placements_available:
        return Signal(key, title, "n/a", "placement data not available (" + "; ".join(data.placement_errors) + ")")
    parts, fired, worst = [], False, []
    for kind in ("channel", "video"):
        items = [p for p in data.placements if p.kind == kind]
        total = sum(p.totals.clicks for p in items)
        flagged = [p for p in items if p.flags]
        hit = sum(p.totals.clicks for p in flagged)
        if not items:
            continue
        share = hit / total if total else 0.0
        parts.append(f"{fmt_pct(share, 1)} of {kind} clicks ({fmt_int(hit)} of {fmt_int(total)})")
        if total >= s.min_clicks and share >= s.flagged_placement_share:
            fired = True
        worst += flagged[:3]
    if not parts:
        return Signal(key, title, "n/a", "no placement rows in this period")
    names = ", ".join(f"{p.name} [{'/'.join(p.flags)}]" for p in worst[:5])
    detail = (
        "on placements whose names look like TV drama or kids content: "
        + "; ".join(parts)
        + f" (threshold {fmt_pct(s.flagged_placement_share, 0)})"
        + (f". Top: {names}" if names else "")
    )
    advice = (
        "review these placements yourself (name matching is crude), then exclude them or tighten content "
        "suitability in Google Ads; this tool does not edit exclusions"
    )
    return Signal(key, title, "fired" if fired else "clear", detail, advice)


def _night(data: AuditData, s: AuditSettings) -> Signal:
    key, title = "night-share", "Night-time share of clicks"
    if not data.hours.available:
        return Signal(key, title, "n/a", "hour data not available (" + "; ".join(data.hours.errors) + ")")
    total = data.hours.total()
    if total.clicks < s.min_clicks:
        return Signal(key, title, "n/a", f"not enough clicks ({fmt_int(total.clicks)}, need {s.min_clicks})")
    night = total_of([t for hour, t in data.hours.rows.items() if _in_window(int(hour), s.night_hours)])
    day = Totals(
        total.impressions - night.impressions, total.clicks - night.clicks, total.cost_micros - night.cost_micros
    )
    share = night.clicks / total.clicks
    ratio = (night.ctr or 0) / day.ctr if day.ctr else None
    start, end = s.night_hours
    detail = (
        f"{fmt_pct(share, 1)} of clicks between {start:02d}:00 and {end:02d}:00 (account time zone), "
        f"night CTR {fmt_pct(night.ctr)} vs day {fmt_pct(day.ctr)} ({fmt_ratio(ratio)}); thresholds "
        f"{fmt_pct(s.night_click_share, 0)} share and {s.night_ctr_ratio:.1f}x CTR"
    )
    fired = share >= s.night_click_share and (ratio is None or ratio >= s.night_ctr_ratio)
    advice = "compare conversions by hour; if night clicks do not convert, add an ad schedule that skips those hours"
    return Signal(key, title, "fired" if fired else "clear", detail, advice)


def _trend(data: AuditData, s: AuditSettings) -> Signal:
    key, title = "ctr-up-cpc-down", "CTR rising while CPC falls"
    if not data.days.available:
        return Signal(key, title, "n/a", "daily data not available (" + "; ".join(data.days.errors) + ")")
    days = [data.days.rows[d] for d in sorted(data.days.rows) if data.days.rows[d].impressions > 0]
    if len(days) < s.min_days:
        return Signal(key, title, "n/a", f"only {len(days)} days with impressions (need {s.min_days})")
    k = max(2, len(days) // 3)
    first, last = total_of(days[:k]), total_of(days[-k:])
    if not (first.clicks and last.clicks and first.ctr and first.cpc):
        return Signal(key, title, "n/a", "no clicks at the start or end of the period")
    ctr_change = (last.ctr or 0) / first.ctr - 1
    cpc_change = (last.cpc or 0) / first.cpc - 1
    detail = (
        f"first {k} days vs last {k} days: CTR {fmt_pct(first.ctr)} -> {fmt_pct(last.ctr)} ({ctr_change:+.0%}), "
        f"CPC {first.cpc:.2f} -> {last.cpc or 0:.2f} ({cpc_change:+.0%}); thresholds CTR +{s.ctr_rise:.0%}, "
        f"CPC -{s.cpc_fall:.0%}"
    )
    fired = ctr_change >= s.ctr_rise and cpc_change <= -s.cpc_fall
    advice = "cheaper and clickier is not better by itself: check that conversions per cost held up before scaling"
    return Signal(key, title, "fired" if fired else "clear", detail, advice)


def _ages(data: AuditData, s: AuditSettings) -> Signal:
    key, title = "age-undetermined", "Clicks from viewers of unknown age"
    if not data.ages.available:
        return Signal(key, title, "n/a", "age data not available (" + "; ".join(data.ages.errors) + ")", core=False)
    total = data.ages.total()
    if total.clicks < s.min_clicks:
        return Signal(key, title, "n/a", f"not enough clicks ({fmt_int(total.clicks)})", core=False)
    unknown = data.ages.rows.get("AGE_RANGE_UNDETERMINED", Totals())
    share = unknown.clicks / total.clicks
    detail = (
        f"{fmt_pct(share, 1)} of clicks from viewers Google could not place in an age range "
        "(signed out, shared devices)"
    )
    fired = share >= s.age_undetermined_share
    return Signal(key, title, "fired" if fired else "clear", detail, "", core=False)


def analyze(data: AuditData, settings: AuditSettings) -> list[Signal]:
    return [
        _instream(data, settings),
        _placements(data, settings),
        _night(data, settings),
        _trend(data, settings),
        _ages(data, settings),
    ]


def verdict(signals: list[Signal]) -> str:
    core = [s for s in signals if s.core]
    fired = [s for s in core if s.status == "fired"]
    available = [s for s in core if s.status != "n/a"]
    if not available:
        return "Not enough data for any signal."
    if not fired:
        return f"No signs of accidental clicks in the {len(available)} signal(s) that could be checked."
    if len(fired) == 1:
        return "One sign of low-intent or accidental clicks. Look closer before spending more."
    return (
        f"{len(fired)} of {len(available)} signals point to low-intent or accidental clicks. "
        "Check conversions per channel before spending more."
    )


# ---------------------------------------------------------------------- command
def _breakdown_rows(breakdown: Breakdown, keys: list[str], currency: str | None) -> list[list[str]]:
    total = breakdown.total()
    out = []
    for name in keys:
        t = breakdown.rows[name]
        share = t.clicks / total.clicks if total.clicks else None
        out.append(
            [
                name,
                fmt_int(t.impressions),
                fmt_int(t.clicks),
                fmt_pct(share, 1),
                fmt_pct(t.ctr),
                "-" if t.cpc is None else f"{t.cpc:.2f}",
                f"{t.cost_micros / 1_000_000:,.2f}",
            ]
        )
    return out


HEADERS = ["impressions", "clicks", "click share", "CTR", "CPC", "cost"]


def cmd_audit(ctx: Context, args: argparse.Namespace) -> int:
    client = make_client(ctx, args)
    today, tz = account_today(ctx, client)
    start, end = resolve_range(
        days=args.days, date_from=args.date_from, date_to=args.date_to, today=today, default_days=14
    )
    campaign_id = gaql_id(args.campaign, "campaign id") if args.campaign else None
    settings = ctx.settings.audit
    data = fetch(client, settings, campaign_id, start, end)
    signals = analyze(data, settings)
    out = ctx.out
    scope = f"campaign {campaign_id}" if campaign_id else "all campaigns"
    out.line(f"Click-quality audit  {fmt_cid(client.customer_id)}  {scope}  {start} .. {end}  ({tz}, {data.currency})")
    out.line("Heuristic: a fired signal is a reason to look closer, not proof of accidental or invalid clicks.")

    out.line(f"\nChannels ({data.channels.source or 'not available'})")
    if data.channels.rows:
        keys = sorted(data.channels.rows, key=lambda k: data.channels.rows[k].clicks, reverse=True)
        out.table(["channel", *HEADERS], _breakdown_rows(data.channels, keys, data.currency), numeric=range(1, 7))
    for error in data.channels.errors:
        out.line(f"  unavailable: {error}")

    top = max(1, args.top)
    for kind in ("channel", "video"):
        items = [p for p in data.placements if p.kind == kind][:top]
        if not items:
            continue
        out.line(f"\nTop {len(items)} placements ({kind}) by clicks")
        out.table(
            ["clicks", "CTR", "cost", "flags", "name"],
            [
                [
                    fmt_int(p.totals.clicks),
                    fmt_pct(p.totals.ctr),
                    f"{p.totals.cost_micros / 1_000_000:,.2f}",
                    "/".join(p.flags) or "-",
                    p.name[:70],
                ]
                for p in items
            ],
            numeric=[0, 1, 2],
        )
    for error in data.placement_errors:
        out.line(f"  unavailable: {error}")

    if data.hours.rows:
        start_h, end_h = settings.night_hours
        out.line(f"\nHours (account time zone; * = night, {start_h:02d}:00-{end_h:02d}:00)")
        keys = sorted(data.hours.rows, key=int)
        rows = _breakdown_rows(data.hours, keys, data.currency)
        for row in rows:
            row[0] = f"{int(row[0]):02d}{' *' if _in_window(int(row[0]), settings.night_hours) else ''}"
        out.table(["hour", *HEADERS], rows, numeric=range(1, 7))
    if data.ages.rows:
        out.line("\nAge ranges")
        keys = sorted(data.ages.rows)
        rows = _breakdown_rows(data.ages, keys, data.currency)
        for row in rows:
            row[0] = row[0].replace("AGE_RANGE_", "").replace("_", "-").lower()
        out.table(["age", *HEADERS], rows, numeric=range(1, 7))
    if data.days.rows:
        out.line("\nDays")
        out.table(
            ["date", *HEADERS], _breakdown_rows(data.days, sorted(data.days.rows), data.currency), numeric=range(1, 7)
        )

    out.line("\nSignals")
    for signal in signals:
        tag = {"fired": "FIRED", "clear": "ok", "n/a": "n/a"}[signal.status]
        if not signal.core and signal.status == "fired":
            tag = "info"
        out.line(f"  [{tag:5}] {signal.title}")
        out.line(f"          {signal.detail}")
        if signal.status == "fired" and signal.core and signal.advice:
            out.line(f"          -> {signal.advice}")
    summary = verdict(signals)
    out.line(f"\nVerdict: {summary}")

    def totals_map(b: Breakdown) -> dict[str, Any]:
        return {name: t.as_dict() for name, t in b.rows.items()}

    out.emit(
        {
            "customer_id": client.customer_id,
            "campaign_id": campaign_id,
            "from": start,
            "to": end,
            "time_zone": tz,
            "currency": data.currency,
            "heuristic": True,
            "channels": {
                "source": data.channels.source,
                "rows": totals_map(data.channels),
                "errors": data.channels.errors,
            },
            "placements": [
                {"kind": p.kind, "name": p.name, "placement": p.placement, "type": p.placement_type, "url": p.url,
                 "flags": p.flags, **p.totals.as_dict()}
                for p in data.placements
            ],
            "placement_errors": data.placement_errors,
            "hours": totals_map(data.hours),
            "ages": totals_map(data.ages),
            "days": totals_map(data.days),
            "signals": [s.__dict__ for s in signals],
            "verdict": summary,
        }
    )  # fmt: skip
    fired = any(s.status == "fired" and s.core for s in signals)
    return ExitCode.REFUSED if (fired and args.fail_on_signal) else ExitCode.OK
