"""spend-watch: poll account spend and act once a target is reached.

Two counters are read:

* the **billing counter**, ``account_budget.amount_served_micros`` of the
  active approved account budget (accounts on monthly invoicing), and
* the **real-time metrics**, the sum of ``metrics.cost_micros`` since that
  budget started.

The billing counter can lag behind the metrics for hours (observed in October
2026), so by default the larger of the two is used. With ``--since`` only the
metrics are counted, from that date: the billing counter always counts from
the budget's own start, so comparing it with a later date would fire early.
Without an active account budget (accounts on automatic payments) there is
no start date to count from, and ``--since`` is required: the watch refuses
to count all-time spend.

When the target is reached, the configured actions run through the normal
plan -> validate -> apply -> read back path, once, pauses first. Each action is
tried on its own, so one failure does not keep the others from running. The
state file remembers that the watch fired, so a restarted watch never acts
twice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from adops_guard.changes import validate_without_writing
from adops_guard.context import Context
from adops_guard.dates import account_now, parse_date
from adops_guard.errors import AdopsError, ApiError, ConfigError, ExitCode, GuardRefused, NetworkError, RateLimited
from adops_guard.google.client import GoogleAdsClient
from adops_guard.google.commands import (
    Prepared,
    customer_info,
    fmt_cid,
    make_client,
    prepare_budget,
    prepare_status,
    run_prepared,
)
from adops_guard.google.gaql import between, gaql_id
from adops_guard.money import fmt_amount, from_micros, parse_amount

MAX_POLL_FAILURES = 10
MIN_INTERVAL = 60
ACCOUNT_BUDGET_QUERY = (
    "SELECT account_budget.id, account_budget.status, account_budget.amount_served_micros, "
    "account_budget.approved_spending_limit_micros, account_budget.approved_start_date_time, "
    "account_budget.approved_end_date_time FROM account_budget WHERE account_budget.status = 'APPROVED'"
)
SINCE_HINT = "pass --since YYYY-MM-DD (the first day to count, in the account's time zone), e.g. the 1st of this month"


@dataclass(frozen=True)
class Action:
    kind: str  # "budget" or "pause"
    campaign_id: int
    amount: Decimal | None = None

    def label(self) -> str:
        if self.kind == "budget":
            return f"set campaign {self.campaign_id} daily budget to {self.amount}"
        return f"pause campaign {self.campaign_id}"


def parse_actions(budgets: list[str] | None, pauses: list[str] | None) -> list[Action]:
    """Pauses first (they are the hard stop), then budget changes, each in the order given."""
    actions: list[Action] = []
    for text in pauses or []:
        actions.append(Action("pause", gaql_id(text, "campaign id")))
    for text in budgets or []:
        campaign, sep, amount = text.partition("=")
        if not sep:
            raise ConfigError(f"--then-budget must look like CAMPAIGN_ID=AMOUNT (got {text!r})")
        actions.append(Action("budget", gaql_id(campaign, "campaign id"), parse_amount(amount, "--then-budget amount")))
    return actions


def watch_key(customer_id: str, target: Decimal, actions: list[Action]) -> str:
    blob = json.dumps([[a.kind, a.campaign_id, str(a.amount)] for a in actions])
    return f"{customer_id}:{target}:{hashlib.sha256(blob.encode()).hexdigest()[:12]}"


@dataclass
class Reading:
    billing: Decimal | None
    metrics: Decimal | None
    since: date | None
    limit: Decimal | None
    billing_note: str | None = None

    def spend(self, source: str) -> Decimal | None:
        if source == "billing":
            return self.billing
        if source == "metrics":
            return self.metrics
        values = [v for v in (self.billing, self.metrics) if v is not None]
        return max(values) if values else None


def _parse_dt(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.strip())
    except ValueError:
        return None


def account_budget_rows(client: GoogleAdsClient) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The approved account budgets, or (None, reason) if the API refuses the query.

    Temporary failures (HTTP 5xx, rate limits) are raised, so the poll is retried
    later; a refusal (no permission, unsupported) is returned as a reason.
    """
    try:
        return client.search(ACCOUNT_BUDGET_QUERY), None
    except RateLimited:
        raise
    except ApiError as exc:
        if exc.status is not None and exc.status >= 500:
            raise
        return None, str(exc)


def active_account_budget(
    rows: list[dict[str, Any]] | None, local_now: datetime
) -> tuple[datetime, dict[str, Any]] | None:
    """The approved account budget running now (the latest-starting one), with its start, or None."""
    naive_now = local_now.replace(tzinfo=None)
    active = []
    for row in rows or []:
        b = row.get("accountBudget") or {}
        start, end = _parse_dt(b.get("approvedStartDateTime")), _parse_dt(b.get("approvedEndDateTime"))
        if start and start <= naive_now and (end is None or end > naive_now):
            active.append((start, b))
    return max(active, key=lambda item: item[0]) if active else None


def metrics_spend(client: GoogleAdsClient, start: date, end: date) -> Decimal:
    """The sum of ``metrics.cost_micros`` from ``start`` to ``end`` (account time zone)."""
    rows = client.search(f"SELECT metrics.cost_micros FROM customer WHERE {between(start, end)}")
    return sum((from_micros((r.get("metrics") or {}).get("costMicros")) for r in rows), Decimal(0))


def read_spend(client: GoogleAdsClient, source: str, since: date | None, local_now: datetime) -> Reading:
    """Read the spend counters. ``since`` given: metrics only, from that date (the billing counter is not read)."""
    billing = limit = None
    budget_start: date | None = None
    note = None
    if since is None:
        rows, error = account_budget_rows(client)
        found = active_account_budget(rows, local_now)
        if found:
            start, b = found
            billing = from_micros(b.get("amountServedMicros"))
            limit = from_micros(b["approvedSpendingLimitMicros"]) if b.get("approvedSpendingLimitMicros") else None
            budget_start = start.date()
        else:
            note = error or "no active approved account budget (normal for automatic payments)"
            if source == "billing":
                raise GuardRefused(f"the billing counter is not available: {note}", hint="use --source metrics")
            # Fail closed: without a start date the metrics would count all-time spend and fire at once.
            raise ConfigError(f"no start date to count spend from: {note}", hint=SINCE_HINT)
    window = since or budget_start
    assert window is not None
    metrics = metrics_spend(client, window, local_now.date()) if source in ("max", "metrics") else None
    return Reading(billing, metrics, window, limit, note)


def _prepare(ctx: Context, client: GoogleAdsClient, action: Action, args: argparse.Namespace) -> Prepared:
    if action.kind == "budget":
        assert action.amount is not None
        return prepare_budget(
            ctx, client, action.campaign_id, action.amount, override=args.override_limit, shared_ok=args.shared_ok
        )
    return prepare_status(ctx, client, "campaign", str(action.campaign_id), "PAUSED", override=False)


def cmd_spend_watch(ctx: Context, args: argparse.Namespace) -> int:
    out = ctx.out
    target = parse_amount(args.target, "--target")
    if target <= 0:
        raise ConfigError("--target must be above zero")
    if not args.once and args.interval < MIN_INTERVAL:
        raise ConfigError(f"--interval must be at least {MIN_INTERVAL} seconds")
    if args.max_hours is not None and args.max_hours <= 0:
        raise ConfigError("--max-hours must be above zero")
    since = parse_date(args.since, "--since") if args.since else None
    if since and args.source == "billing":
        raise ConfigError(
            "--since applies to the metrics counter; the billing counter counts from its account budget's start",
            hint="use --source metrics or --source max with --since, or drop --since",
        )
    actions = parse_actions(args.then_budget, args.then_pause)
    client = make_client(ctx, args)
    info = customer_info(client)
    currency, tz = info.get("currencyCode"), info.get("timeZone")
    if since and since > account_now(tz, ctx.now())[0].date():
        raise ConfigError(f"--since {since} is in the future (account time zone {tz})")
    key = watch_key(client.customer_id or "", target, actions)

    if ctx.settings.state_dir.exists():
        fired = (ctx.store().load().get("spend_watch") or {}).get(key)
        if fired and fired.get("applied_at") and not args.reset:
            out.line(f"NO-OP    this spend watch already fired at {fired['applied_at']} (spend {fired.get('spend')})")
            out.line("         pass --reset to arm it again")
            out.emit({"noop": True, "already_fired": fired})
            return ExitCode.OK

    out.line(
        f"WATCH    {fmt_cid(client.customer_id)}: target {fmt_amount(target, currency)} "
        f"(source: {args.source}{', since ' + str(since) if since else ''})"
    )
    if since and args.source == "max":
        out.line("         (billing counter ignored: --since given, so only the metrics count)")
    for action in actions:
        out.line(f"         then: {action.label()}")
    if not actions:
        out.line("         then: nothing (alert only: exit when the target is reached)")
    # Preflight: read, guard-check and validate every action now, not hours later at the target.
    for action in actions:
        prepared = _prepare(ctx, client, action, args)
        if prepared.noop:
            out.line(f"CHECK    {prepared.description}: {prepared.noop_reason}")
            continue
        operation = prepared.operation
        assert operation is not None
        validate_without_writing(
            ctx,
            platform="google",
            action=prepared.action,
            target=prepared.target,
            validate=lambda p=prepared, op=operation: client.mutate(p.service, [op], validate_only=True),
            read_back=prepared.read_back,
            show=prepared.show,
        )
        out.line(f"CHECK    {prepared.description}: validated (validate only)")
        for note in prepared.notes:
            out.line(f"         {note}")
    if not args.apply and actions:
        out.line("DRY RUN  at the target this will only print what it would do. Add --apply to act.")

    deadline = ctx.monotonic() + args.max_hours * 3600 if args.max_hours is not None else None
    failures = 0
    while True:
        local_now, _ = account_now(tz, ctx.now())
        try:
            reading = read_spend(client, args.source, since, local_now)
        except GuardRefused:
            raise
        except (ApiError, NetworkError) as exc:
            if args.once:  # a single poll (cron): fail now, the next run tries again
                raise
            failures += 1
            out.warn(f"poll failed ({failures}/{MAX_POLL_FAILURES}): {exc}")
            if failures >= MAX_POLL_FAILURES:
                raise
            ctx.sleep(args.interval)
            continue
        failures = 0
        spend = reading.spend(args.source)
        percent = f"{spend / target:.0%}" if spend is not None else "?"
        out.line(
            f"{ctx.now().strftime('%Y-%m-%d %H:%M')} UTC  spend {fmt_amount(spend, currency)} ({percent} of target)"
            + (f"  billing {fmt_amount(reading.billing)}" if since is None else "")
            + (f"  metrics {fmt_amount(reading.metrics)} since {reading.since}" if reading.metrics is not None else "")
            + (f"  account limit {fmt_amount(reading.limit)}" if reading.limit is not None else "")
        )
        if spend is not None and spend >= target:
            return _on_target(ctx, client, args, actions, key, spend, target, currency)
        if args.once:
            out.emit({"target_reached": False, "spend": spend, "target": target, "billing": reading.billing,
                      "metrics": reading.metrics, "since": reading.since})  # fmt: skip
            return ExitCode.OK
        if deadline is not None and ctx.monotonic() >= deadline:
            out.line("STOP     --max-hours reached before the target; nothing was changed")
            out.emit({"target_reached": False, "spend": spend, "target": target, "stopped": "max-hours"})
            return ExitCode.OK
        ctx.sleep(args.interval)


def _on_target(
    ctx: Context,
    client: GoogleAdsClient,
    args: argparse.Namespace,
    actions: list[Action],
    key: str,
    spend: Decimal,
    target: Decimal,
    currency: str | None,
) -> int:
    out = ctx.out
    out.line(f"TARGET   reached: {fmt_amount(spend, currency)} >= {fmt_amount(target, currency)}")
    result: dict[str, Any] = {"target_reached": True, "spend": spend, "target": target, "actions": []}
    if not actions:
        out.emit(result)
        return ExitCode.OK
    if not args.apply:
        for action in actions:
            out.line(f"DRY RUN  would {action.label()}")
        out.emit({**result, "applied": False})
        return ExitCode.OK
    worst = ExitCode.OK
    with ctx.lock("google spend-watch"), out.nested():
        for action in actions:  # each on its own: one failure must not keep the others (pauses first) from running
            try:
                prepared = _prepare(ctx, client, action, args)  # fresh read: values may have changed meanwhile
                code = run_prepared(ctx, client, prepared, apply_flag=True)
            except AdopsError as exc:
                out.error(f"{action.label()}: {exc}")
                if exc.hint:
                    out.error("hint: " + exc.hint)
                code = exc.exit_code
            result["actions"].append({"action": action.label(), "exit_code": int(code)})
            worst = max(worst, code)
        if worst == ExitCode.OK:
            stamp = ctx.now().strftime("%Y-%m-%dT%H:%M:%SZ")
            ctx.store().update(
                lambda d: d.setdefault("spend_watch", {}).__setitem__(
                    key,
                    {
                        "applied_at": stamp,
                        "spend": str(spend),
                        "target": str(target),
                        "actions": [a.label() for a in actions],
                    },  # fmt: skip
                )
            )
    out.emit({**result, "applied": worst == ExitCode.OK})
    return int(worst)
