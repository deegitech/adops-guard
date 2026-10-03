"""The ``adops-guard`` command line."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from adops_guard import __version__, doctor
from adops_guard.config import TEST_ENDPOINTS_ENV, load_settings
from adops_guard.context import Context
from adops_guard.dates import utc_now
from adops_guard.errors import AdopsError, ExitCode
from adops_guard.google import audit as g_audit
from adops_guard.google import commands as g
from adops_guard.google import watch as g_watch
from adops_guard.meta import commands as m
from adops_guard.output import Output
from adops_guard.redact import redact

DESCRIPTION = """\
Guard-railed ad operations for the Google Ads API and the Meta Marketing API.

Every write is a dry run unless you pass --apply: the tool prints the plan, asks
the API to validate it, and stops. With --apply it writes, reads the object back,
compares, and journals each step. Spend increases above the ceilings in your
config file are refused.

New here? Follow docs/setup.md, then run: adops-guard doctor
"""

EPILOG = """\
exit codes: 0 ok, 1 error (or a doctor check failed), 2 usage/config,
            3 refused by a guard or a check, 4 read-back mismatch,
            5 rate limited (retry later)
docs: https://github.com/deegitech/adops-guard (setup: docs/setup.md,
      errors and fixes: docs/troubleshooting.md)
"""

DOCTOR_DESCRIPTION = """\
Check the whole setup without changing anything, in the order you set it up:
the config file, then Google Ads (credentials, OAuth, account access, account
budget, auto-apply, conversion goals), then Meta (token, permissions, app,
ad account, spending limit, rate tier, Instagram identity, advertisable apps).
Every problem comes with its fix: a menu path, a permission name or a command.
A platform that is not configured is skipped, unless you name it.

Secrets are never printed. In a terminal the checks are marked with check
marks and crosses; in logs and pipes with plain ASCII words.

exit codes: 0 when no check failed (warnings allowed), 1 when one did
guide: docs/setup.md   errors and fixes: docs/troubleshooting.md
"""


def _common() -> argparse.ArgumentParser:
    # Repeated on every subcommand with SUPPRESS defaults, so options work before or after the command.
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default=argparse.SUPPRESS, help="config file (default: ./adops-guard.ini)")
    parser.add_argument("--state-dir", default=argparse.SUPPRESS, help="journal/state directory")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON on stdout")
    parser.add_argument("--debug", action="store_true", default=argparse.SUPPRESS, help="redacted traceback on errors")
    return parser


def _google_common() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--customer-id", default=argparse.SUPPRESS, help="Google Ads account, e.g. 123-456-7890")
    parser.add_argument("--login-customer-id", default=argparse.SUPPRESS, help="manager (MCC) account, if any")
    return parser


def _meta_common() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ad-account", default=argparse.SUPPRESS, help="Meta ad account, e.g. act_123456789012345")
    return parser


def _write_flags(parser: argparse.ArgumentParser, *, limit: bool = True, validate: bool = False) -> None:
    parser.add_argument("--apply", action="store_true", help="really make the change (default: dry run)")
    if limit:
        parser.add_argument(
            "--override-limit", action="store_true", help="skip the configured spend ceiling for this one change"
        )
    if validate:
        parser.add_argument(
            "--validate",
            action="store_true",
            help="dry run: also have Meta check the request (a validate-only POST); --apply always does this first",
        )


def _range_flags(parser: argparse.ArgumentParser, default_days: int) -> None:
    parser.add_argument("--days", type=int, help=f"last N days including today (default {default_days})")
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="first day (instead of --days)")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="last day (default: today)")


def _level_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--campaign", metavar="ID", help="campaign id")
    group.add_argument("--ad-group", metavar="ID", help="ad group id")
    group.add_argument("--ad", metavar="AD_GROUP_ID~AD_ID", help="ad, as ad group id ~ ad id")


CHANNEL_LABELS = {
    "youtube_in_stream": "YouTube in-stream",
    "youtube_in_feed": "YouTube in-feed",
    "youtube_shorts": "YouTube Shorts",
    "discover": "Discover",
    "gmail": "Gmail",
    "display": "Display",
}

SPEND_WATCH_DESCRIPTION = """\
Poll the account's spend and, once it reaches --target, run each action once.

Spend is counted from the start of the active account budget (monthly
invoicing), or from --since. Without an active account budget (automatic
payments) --since is required: the watch refuses to count all-time spend.
With --since only the real-time metrics count (the billing counter has its
own start date). At the target, pauses run first, then budget changes, each
on its own. That the watch fired is remembered in state.json, so a restart
never acts twice; --reset arms it again.
"""


def build_parser() -> argparse.ArgumentParser:
    common, gcommon, mcommon = _common(), _google_common(), _meta_common()
    parser = argparse.ArgumentParser(
        prog="adops-guard",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default=None, help="config file (default: ./adops-guard.ini)")
    parser.add_argument("--state-dir", default=None, help="journal/state directory (default: .adops-guard)")
    parser.add_argument("--json", action="store_true", default=False, help="machine-readable JSON on stdout")
    parser.add_argument("--debug", action="store_true", default=False, help="show a (redacted) traceback on errors")
    platforms = parser.add_subparsers(dest="platform", required=True, metavar="{google,meta,doctor}")

    # ------------------------------------------------------------------ google
    gp = platforms.add_parser(
        "google",
        parents=[common, gcommon],
        help="Google Ads API (REST, no client library)",
        description="Google Ads API commands (REST). Writes are dry runs unless --apply is given.",
    )
    gs = gp.add_subparsers(dest="command", required=True, metavar="COMMAND")
    parents = [common, gcommon]

    def google_command(name: str, help_text: str, description: str) -> argparse.ArgumentParser:
        return gs.add_parser(name, parents=parents, help=help_text, description=description)

    p = google_command(
        "accounts",
        "accounts the OAuth user can reach",
        "List the accounts the OAuth user can reach, and the accounts under the manager account (if one is set).",
    )
    p.set_defaults(func=g.cmd_accounts)

    p = google_command(
        "status",
        "account, account budgets and campaigns",
        "Show the account, its account budgets (monthly invoicing) and its campaigns with budgets and 30-day cost.",
    )
    p.set_defaults(func=g.cmd_status)

    p = google_command(
        "report",
        "clicks, cost and conversions by day or campaign",
        "Impressions, clicks, CTR, CPC, cost and conversions by day or by campaign, in the account's time zone.",
    )
    p.add_argument("--by", choices=["day", "campaign"], default="day", help="one row per day (default) or campaign")
    p.add_argument("--campaign", metavar="ID", help="only this campaign")
    _range_flags(p, 7)
    p.set_defaults(func=g.cmd_report)

    budget = google_command("budget", "campaign budgets", "Change campaign budgets.")
    bs = budget.add_subparsers(dest="budget_command", required=True, metavar="ACTION")
    p = bs.add_parser(
        "set",
        parents=parents,
        help="set a campaign's daily budget",
        description="Set a campaign's daily budget. Raising it above [google] max_daily_budget is refused.",
    )
    p.add_argument("--campaign", metavar="ID", required=True, help="campaign id")
    p.add_argument("--amount", required=True, help="daily amount in the account currency, e.g. 25.00")
    p.add_argument("--shared-ok", action="store_true", help="allow changing a budget shared by several campaigns")
    _write_flags(p)
    p.set_defaults(func=g.cmd_budget_set)

    p = google_command(
        "pause",
        "pause a campaign, ad group or ad",
        "Pause a campaign, ad group or ad. A REMOVED one does not serve, so it is left as it is.",
    )
    _level_flags(p)
    _write_flags(p, limit=False)
    p.set_defaults(func=g.cmd_pause)

    p = google_command(
        "enable",
        "enable a campaign, ad group or ad (spends money)",
        "Enable a campaign, ad group or ad. The campaign's daily budget is checked against "
        "[google] max_daily_budget first.",
    )
    _level_flags(p)
    _write_flags(p)
    p.set_defaults(func=g.cmd_enable)

    p = google_command(
        "channel-controls",
        "Demand Gen: turn YouTube/Discover/Gmail/Display on or off",
        "Show or change the channels of a Demand Gen ad group. Without channel options it only shows them; "
        "only the channels you name are sent (one leaf field mask each).",
    )
    p.add_argument("--ad-group", metavar="ID", required=True, help="ad group id")
    for name, _ in g.CHANNELS:
        p.add_argument("--" + name.replace("_", "-"), dest=name, choices=["on", "off"], help=CHANNEL_LABELS[name])
    _write_flags(p, limit=False)
    p.set_defaults(func=g.cmd_channel_controls)

    p = google_command(
        "audit",
        "click-quality audit (heuristic signs of accidental clicks)",
        "Read-only click-quality audit: channel, placement, hour and age breakdowns and four heuristic signals "
        "(see docs/click-quality-audit.md).",
    )
    p.add_argument("--campaign", metavar="ID", help="only this campaign (default: the whole account)")
    p.add_argument("--top", type=int, default=10, help="placements to list (default 10)")
    p.add_argument("--fail-on-signal", action="store_true", help="exit 3 if any signal fires (for cron/CI)")
    _range_flags(p, 14)
    p.set_defaults(func=g_audit.cmd_audit)

    p = gs.add_parser(
        "spend-watch",
        parents=parents,
        help="poll spend; at a target, change budgets or pause",
        description=SPEND_WATCH_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--target", required=True, help="total spend that triggers the actions, e.g. 1500.00")
    p.add_argument(
        "--then-budget", action="append", metavar="CAMPAIGN_ID=AMOUNT", help="set this daily budget (repeatable)"
    )
    p.add_argument("--then-pause", action="append", metavar="CAMPAIGN_ID", help="pause this campaign (repeatable)")
    p.add_argument("--source", choices=["max", "billing", "metrics"], default="max",
                   help="spend counter: larger of both (default), billing counter only, or metrics only")  # fmt: skip
    p.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="count metrics spend from this day (account time zone); the billing counter is then ignored. "
        "Default: the active account budget's start; required without one",
    )
    p.add_argument("--interval", type=int, default=900, help="seconds between polls (default 900, minimum 60)")
    p.add_argument(
        "--max-hours", type=float, help="stop after this many hours without reaching the target (counted per start)"
    )
    p.add_argument("--once", action="store_true", help="poll once and exit (for cron)")
    p.add_argument("--reset", action="store_true", help="arm again a watch that already fired")
    p.add_argument("--shared-ok", action="store_true", help="allow changing a shared budget")
    _write_flags(p)
    p.set_defaults(func=g_watch.cmd_spend_watch)

    conv = google_command("conversion-action", "website conversion actions", "List or create conversion actions.")
    cs = conv.add_subparsers(dest="conversion_command", required=True, metavar="ACTION")
    p = cs.add_parser(
        "list", parents=parents, help="list conversion actions", description="List the account's conversion actions."
    )
    p.set_defaults(func=g.cmd_conversion_list)
    p = cs.add_parser(
        "create",
        parents=parents,
        help="create a WEBPAGE conversion action (e.g. outbound click)",
        description="Create a WEBPAGE conversion action and print its send_to value for your landing page. "
        "An action with the same name is reused, never duplicated.",
    )
    p.add_argument("--name", required=True, help="conversion action name (1 to 100 characters)")
    p.add_argument("--category", default="OUTBOUND_CLICK", help="conversion category (default OUTBOUND_CLICK)")
    p.add_argument("--counting", choices=["one", "many"], default="one", help="per click (default one)")
    p.add_argument("--value", default="1", help="default conversion value (default 1)")
    p.add_argument("--primary", action="store_true", help="make it a primary (bidding) action; default secondary")
    p.add_argument("--click-lookback-days", type=int, default=30, help="click-through window, 1-90 days (default 30)")
    p.add_argument("--view-lookback-days", type=int, default=1, help="view-through window, 1-30 days (default 1)")
    _write_flags(p, limit=False)
    p.set_defaults(func=g.cmd_conversion_create)

    # ------------------------------------------------------------------ meta
    mp = platforms.add_parser(
        "meta",
        parents=[common, mcommon],
        help="Meta Marketing API (Graph API)",
        description="Meta Marketing API commands (Graph API). Writes are dry runs unless --apply is given, and "
        "only touch objects in the configured ad account.",
    )
    ms = mp.add_subparsers(dest="command", required=True, metavar="COMMAND")
    parents = [common, mcommon]

    def meta_command(name: str, help_text: str, description: str) -> argparse.ArgumentParser:
        return ms.add_parser(name, parents=parents, help=help_text, description=description)

    p = meta_command(
        "whoami",
        "token user, permissions and the ad account",
        "Show the token's user, its granted permissions and the configured ad account.",
    )
    p.set_defaults(func=m.cmd_whoami)

    p = meta_command(
        "status",
        "ad account and campaigns (and one campaign's ad sets/ads)",
        "Show the ad account and its campaigns; with --campaign also that campaign's ad sets and ads.",
    )
    p.add_argument("--campaign", metavar="ID", help="also list this campaign's ad sets and ads")
    p.set_defaults(func=m.cmd_status)

    p = meta_command(
        "report",
        "spend, impressions, clicks by level and day",
        "Spend, impressions, reach, clicks, CTR and CPC from the Insights API, in the ad account's time zone.",
    )
    p.add_argument("--level", choices=["account", "campaign", "adset", "ad"], default="campaign", help="row level")
    p.add_argument("--by-day", action="store_true", help="one row per day")
    _range_flags(p, 7)
    p.set_defaults(func=m.cmd_report)

    setp = meta_command("set", "set status or budget, with read-back", "Set a status or a budget, with read-back.")
    ss = setp.add_subparsers(dest="set_command", required=True, metavar="WHAT")
    p = ss.add_parser(
        "status",
        parents=parents,
        help="ACTIVE or PAUSED for a campaign, ad set or ad",
        description="Set a campaign, ad set or ad to ACTIVE or PAUSED. Activating checks the budget that would "
        "spend against the ceilings first.",
    )
    p.add_argument("object_id", metavar="ID", help="campaign, ad set or ad id")
    p.add_argument("status", choices=["ACTIVE", "PAUSED"], help="the new status")
    p.add_argument("--type", choices=["campaign", "adset", "ad"], help="assert the object type")
    _write_flags(p, validate=True)
    p.set_defaults(func=m.cmd_set_status)
    p = ss.add_parser(
        "budget",
        parents=parents,
        help="daily or lifetime budget of a campaign or ad set",
        description="Set the daily or lifetime budget of a campaign or ad set. Raising it above the configured "
        "ceiling is refused.",
    )
    p.add_argument("object_id", metavar="ID", help="campaign or ad set id")
    amount = p.add_mutually_exclusive_group(required=True)
    amount.add_argument("--daily", metavar="AMOUNT", help="daily budget in the account currency, e.g. 20.00")
    amount.add_argument("--lifetime", metavar="AMOUNT", help="lifetime budget in the account currency")
    p.add_argument("--type", choices=["campaign", "adset"], help="assert the object type")
    _write_flags(p, validate=True)
    p.set_defaults(func=m.cmd_set_budget)

    p = meta_command(
        "spend-cap",
        "set, lower or remove a campaign spend cap",
        "Set, lower or remove a campaign spend cap. Adding or lowering a cap is always allowed; raising it is "
        "checked against [meta] max_spend_cap; removing it needs --override-limit.",
    )
    p.add_argument("campaign_id", metavar="CAMPAIGN_ID", help="campaign id")
    cap = p.add_mutually_exclusive_group(required=True)
    cap.add_argument("--amount", help="cap in the account currency, e.g. 500.00")
    cap.add_argument("--remove", action="store_true", help="remove the cap (needs --override-limit)")
    _write_flags(p, validate=True)
    p.set_defaults(func=m.cmd_spend_cap)

    p = meta_command(
        "usage",
        "Meta rate-limit usage right now",
        "Show Meta's rate-limit usage headers after one small request.",
    )
    p.set_defaults(func=m.cmd_usage)

    p = meta_command(
        "video-check",
        "Instagram eligibility of ad videos (and local files)",
        "Read is_instagram_eligible for uploaded ad videos, and check the length of local files with ffprobe.",
    )
    p.add_argument("video_ids", nargs="*", metavar="VIDEO_ID", help="uploaded ad video ids")
    p.add_argument("--file", action="append", metavar="PATH", help="local video to check with ffprobe (repeatable)")
    p.add_argument(
        "--max-seconds", type=float, default=15.0, help="flag videos this long or longer (default 15, observed)"
    )
    p.set_defaults(func=m.cmd_video_check)

    # ------------------------------------------------------------------ doctor
    p = platforms.add_parser(
        "doctor",
        parents=[common, gcommon, mcommon],
        help="check the setup (read-only) and print the fix for each problem",
        description=DOCTOR_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "only",
        nargs="?",
        choices=["google", "meta"],
        metavar="PLATFORM",
        help="check only google or meta (default: both; a platform that is not configured is skipped)",
    )
    p.add_argument(
        "--app",
        metavar="APP_ID",
        help="Meta: also check that this app (Meta app id or App Store id) can be advertised from the ad account",
    )
    p.set_defaults(func=None)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    runner: Callable[..., Any] | None = None,
    cwd: Path | None = None,
) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:  # --help, --version, or a usage error (already printed by argparse)
        code = exc.code if isinstance(exc.code, int) else ExitCode.USAGE
        if code and "--json" in raw_argv:  # --json promises one JSON document on stdout, errors included
            usage = {"error": "invalid arguments (details on stderr)", "hint": None, "exit_code": int(code)}
            print(json.dumps(usage, indent=2), file=stdout or sys.stdout)
        return code
    out = Output(json_mode=bool(getattr(args, "json", False)), stdout=stdout, stderr=stderr)
    environment = os.environ if env is None else env
    try:
        if getattr(args, "platform", None) == "doctor":  # loads the config itself: a broken one is reported
            return doctor.run(
                args,
                out=out,
                env=environment,
                cwd=cwd,
                sleep=sleep or time.sleep,
                now=now or utc_now,
                monotonic=monotonic or time.monotonic,
                runner=runner or subprocess.run,
            )
        settings = load_settings(getattr(args, "config", None), environment, cwd)
        if settings.discovered:
            out.note(f"using config {settings.source} (found in the working directory)")
        for name, url in settings.endpoint_overrides():
            out.warn(f"{name} = {url}: a test endpoint ({TEST_ENDPOINTS_ENV}=1); credentials are sent there")
        if getattr(args, "state_dir", None):
            settings = dataclasses.replace(settings, state_dir=Path(args.state_dir).expanduser())
        ctx = Context(
            settings=settings,
            out=out,
            env=environment,
            sleep=sleep or time.sleep,
            now=now or utc_now,
            monotonic=monotonic or time.monotonic,
            runner=runner or subprocess.run,
        )
        return int(args.func(ctx, args))
    except AdopsError as exc:
        out.error(str(exc))
        if exc.hint:
            out.error("hint: " + exc.hint)
        out.emit_error(str(exc), exc.hint, exc.exit_code)
        return int(exc.exit_code)
    except KeyboardInterrupt:
        out.error("interrupted")
        out.emit_error("interrupted", None, ExitCode.INTERRUPTED)
        return int(ExitCode.INTERRUPTED)
    except Exception as exc:  # noqa: BLE001 - last line of defence: never print an unredacted traceback
        if getattr(args, "debug", False):
            print(redact(traceback.format_exc()), file=stderr or sys.stderr)
        message = f"unexpected {type(exc).__name__}: {exc}"
        out.error(f"{message} (re-run with --debug for a redacted traceback)")
        out.emit_error(message, "re-run with --debug for a redacted traceback", ExitCode.ERROR)
        return int(ExitCode.ERROR)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
