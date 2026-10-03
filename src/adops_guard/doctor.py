"""``adops-guard doctor``: read-only checks of the whole setup, each with its fix.

The checks run in the order you set things up (docs/setup.md):

* **Setup:** the config file and the state directory.
* **Google Ads:** the ceiling, the credentials (file mode, required keys), the
  OAuth refresh, the accounts the OAuth user reaches, the configured account
  (status, manager or client, API access level), the account budget and its
  headroom, auto-applied recommendations, conversion goals.
* **Meta:** the ceilings, ffprobe, where the token comes from (never its
  value), the token's user and permissions, the app (what Live mode needs), the
  ad account (status, prepaid or card, spending limit), the rate-limit tier and
  usage, the Instagram identity, and the apps the ad account can advertise.

Every check is a read. Nothing is written to either platform, no lock is taken
and the state directory is not created. Secrets are never printed: the token
checks report where a token comes from and how long it is, never its value,
and all output goes through the redactor.

Every failed check prints a fix. A network error or an HTTP 5xx is reported as
a network or service problem, never as a bad token or a missing permission; an
error without a known fix points at docs/troubleshooting.md (by URL, since a
pipx install has no docs folder).

In a terminal each check is marked ✓ (pass), ✗ (fail), ! (warning), ? (check
by hand), · (information) or – (skipped). In logs and pipes the marks are plain
ASCII: ok, FAIL, WARN, manual, info, skip. The exit code is 0 when no check
failed (warnings and manual checks do not count) and 1 when one did.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import stat
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from adops_guard import __version__
from adops_guard.config import TEST_ENDPOINTS_ENV, MetaSettings, Settings, load_settings
from adops_guard.context import Context
from adops_guard.credentials import (
    GOOGLE_DEFAULT_FILE,
    GOOGLE_ENV,
    GOOGLE_FILE_ENV,
    META_FILE_ENV,
    load_google_credentials,
    load_meta_token,
)
from adops_guard.dates import account_now
from adops_guard.errors import AdopsError, ApiError, ConfigError, CredentialError, ExitCode, NetworkError, RateLimited
from adops_guard.google.client import GoogleAdsClient
from adops_guard.google.commands import customer_info, fmt_cid, try_search
from adops_guard.google.watch import account_budget_rows, active_account_budget, metrics_spend
from adops_guard.hints import CHECK_WITH_DOCTOR, META_TOKEN_RENEW, NETWORK_HINT
from adops_guard.meta.client import MetaClient
from adops_guard.meta.commands import ACCOUNT_STATUS, NO_SPEND_CAP, minor, normalize_account
from adops_guard.money import fmt_amount, from_micros, from_minor, meta_offset
from adops_guard.output import Output
from adops_guard.state import StateStore

PASS, FAIL, WARN, MANUAL, INFO, SKIP = "pass", "fail", "warn", "manual", "info", "skip"
STATUSES = (PASS, FAIL, WARN, MANUAL, INFO, SKIP)
FANCY_MARKS = {PASS: "✓", FAIL: "✗", WARN: "!", MANUAL: "?", INFO: "·", SKIP: "–"}
PLAIN_MARKS = {PASS: "ok", FAIL: "FAIL", WARN: "WARN", MANUAL: "manual", INFO: "info", SKIP: "skip"}
FIX_LABELS = {FAIL: "fix", WARN: "fix", MANUAL: "check"}  # anything else: "tip"

# ---------------------------------------------------------------------- fixes (plain ASCII, ">" between menu steps)
# The fixes name files in docs/; someone who installed with pipx has no checkout, so the doctor prints where they are.
DOCS_URL = f"https://github.com/deegitech/adops-guard/tree/v{__version__}/docs"
TROUBLESHOOTING_URL = f"https://github.com/deegitech/adops-guard/blob/v{__version__}/docs/troubleshooting.md"
GENERIC_FIX = f"look the error up in docs/troubleshooting.md ({TROUBLESHOOTING_URL})"
CONFIG_FIX = (
    "copy examples/adops-guard.example.ini to ./adops-guard.ini, set your ids and ceilings, then run: "
    "chmod go-w adops-guard.ini (docs/setup.md, step 0)"
)
CONFIG_VALUE_FIX = "fix that line of the config file (examples/adops-guard.example.ini shows every key)"
ACCESS_LEVEL_TIP = (
    "the level is shown on Cloud console > Google Ads API > Overview; Explorer allows 2,880 operations a day on "
    "production accounts, Basic 15,000 (it needs brand verification)"
)
ACCOUNT_BUDGET_FIX = (
    "raise it: Google Ads > Billing > Account budget > Edit; campaigns stop at the limit, even with promotional "
    "credit waiting"
)
AUTO_APPLY_FIX = (
    "Google Ads > Campaigns > Recommendations > Auto-apply: turn off the types you manage yourself (Google may "
    "otherwise change settings by itself, e.g. turn channels back on)"
)
AUTO_APPLY_CHECK = "Google Ads > Campaigns > Recommendations > Auto-apply: turn off the types you manage yourself"
CONVERSION_PRIMARY_FIX = (
    "if campaigns should bid on it: Google Ads > Goals > Conversions > Summary > (action) > Edit settings > Primary "
    "action, and set campaign-level goals so other campaigns are not affected; if you only report on it, ignore this"
)
META_PERMISSIONS_FIX = (
    "App dashboard > Use cases > 'Create & manage ads with Marketing API' > Customize: add ads_management and "
    "ads_read (one by one if needed); then Graph API Explorer > Generate Access Token with them ticked, extend it "
    "and store it again (docs/setup.md, Meta steps 1 to 6)"
)
META_OPTIONAL_TIP = (
    "only needed for the Page and Instagram checks and for linking assets; add them in the same use case and "
    "regenerate the token if you want those checks"
)
META_LIVE_FIX = (
    "App dashboard > App settings > Basic: add a privacy policy URL, a category and an app icon, save, then switch "
    "App Mode to Live (or Publish)"
)
META_LIVE_CHECK = (
    "developers.facebook.com > My Apps > (your app): the top bar must say App Mode: Live; in Development mode ad "
    "creatives fail with 100/1885183"
)
META_ACCOUNT_FIX = (
    "give the token's user (or system user) access: Business settings > Accounts > Ad accounts > (account) > "
    "Assign people > Manage campaigns"
)
META_SPENDING_LIMIT_FIX = (
    "Ads Manager > Billing & payments > Account spending limit: set or raise it (adops-guard shows it but never "
    "sets it)"
)
META_TIER_TIP = (
    "for higher limits, upgrade the 'Marketing API Access Tier' feature (older consoles: 'Ads Management Standard "
    "Access'): App dashboard > App Review > Permissions and features"
)
META_IG_LINK_FIX = (
    "link one: Facebook Page > Settings > Linked accounts > Instagram > Connect; then regenerate the token with "
    "the Page AND the Instagram account ticked (docs/setup.md, Meta step 4)"
)
META_IG_CHECK = "Facebook Page > Settings > Linked accounts: an Instagram account should be connected"
META_APP_PROMOTION_FIX = (
    "(1) App dashboard > App settings > Basic > Add platform > iOS: bundle ID and App Store ID; (2) Business "
    "settings > Accounts > Apps > (app) > Add assets > Ad accounts > tick the ad account (it may show only on the "
    "app's Connected assets tab)"
)
META_STATUS_FIX = {
    "DISABLED": "Meta disabled the ad account: request a review at facebook.com/accountquality",
    "UNSETTLED": "the account has an unpaid balance: Ads Manager > Billing & payments > pay it",
    "PENDING_RISK_REVIEW": "Meta is reviewing the account: wait, or check facebook.com/accountquality",
    "PENDING_SETTLEMENT": "a payment is being processed: wait, or check Ads Manager > Billing & payments",
    "IN_GRACE_PERIOD": "a payment failed: update the payment method in Ads Manager > Billing & payments",
    "PENDING_CLOSURE": "the account is being closed: use another ad account",
    "CLOSED": "the account is closed: use another ad account",
}
FFMPEG_TIP = "install FFmpeg: brew install ffmpeg (macOS) or sudo apt install ffmpeg (Debian, Ubuntu)"

AUTO_APPLY_QUERY = (
    "SELECT recommendation_subscription.type, recommendation_subscription.status FROM recommendation_subscription"
)
CONVERSION_QUERY = (
    "SELECT conversion_action.id, conversion_action.name, conversion_action.primary_for_goal "
    "FROM conversion_action WHERE conversion_action.status = 'ENABLED'"
)
APP_FIELDS = "id,name,category,privacy_policy_url,icon_url,supported_platforms,object_store_urls"
LIVE_NEEDS = (("privacy_policy_url", "privacy policy URL"), ("category", "category"), ("icon_url", "app icon"))
ACCOUNT_FIELDS = "id,name,account_status,disable_reason,currency,timezone_name,amount_spent,spend_cap,is_prepay_account"
REQUIRED_PERMISSIONS = ("ads_read", "ads_management")
OPTIONAL_PERMISSIONS = ("business_management", "pages_show_list", "pages_read_engagement")


@dataclass
class Check:
    section: str
    name: str
    status: str
    detail: str
    fix: str | None = None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fancy_marks_for(stream: TextIO) -> bool:
    """✓/✗ for a terminal that can show them; plain ASCII for logs, pipes and files."""
    try:
        if not stream.isatty():
            return False
        "".join(FANCY_MARKS.values()).encode(getattr(stream, "encoding", None) or "ascii")
    except (AttributeError, LookupError, OSError, UnicodeError, ValueError):
        return False
    return True


class Report:
    """Collects the checks, prints each one with its mark and its fix, and builds the JSON document."""

    def __init__(self, out: Output, *, fancy: bool | None = None) -> None:
        self.out = out
        self.fancy = fancy_marks_for(out.human_stream()) if fancy is None else fancy
        self.checks: list[Check] = []
        self.section_name = ""

    def section(self, title: str) -> None:
        self.section_name = title
        self.out.line("")
        self.out.line(title)

    def add(self, status: str, name: str, detail: str, fix: str | None = None) -> None:
        if fix:
            fix = fix.removesuffix(CHECK_WITH_DOCTOR)  # "check with: adops-guard doctor" would point at itself
        if status == FAIL and not fix:
            fix = GENERIC_FIX  # every failed check prints a fix
        self.checks.append(Check(self.section_name, name, status, detail, fix))
        if self.fancy:
            self.out.line(f"  {FANCY_MARKS[status]} {detail}")
            indent = " " * 4
        else:
            self.out.line(f"  {PLAIN_MARKS[status]:<6} {detail}")
            indent = " " * 9
        if fix:
            self.out.line(f"{indent}{FIX_LABELS.get(status, 'tip')}: {fix}")

    @property
    def failed(self) -> bool:
        return any(check.status == FAIL for check in self.checks)

    def finish(self) -> int:
        counts = Counter(check.status for check in self.checks)
        parts = [f"{counts[PASS]} passed", f"{counts[FAIL]} failed", f"{counts[WARN]} warning(s)"]
        if counts[MANUAL]:
            parts.append(f"{counts[MANUAL]} to check by hand")
        if counts[SKIP]:
            parts.append(f"{counts[SKIP]} skipped")
        self.out.line("")
        self.out.line("Result: " + ", ".join(parts))
        if self.failed:
            self.out.line("Fix what failed, then run adops-guard doctor again.")
            self.out.line(f"Every error and its fix: {TROUBLESHOOTING_URL}")
        if any("docs/" in (check.fix or "") for check in self.checks):
            self.out.line(f"The docs/ files named above: {DOCS_URL}")
        self.out.emit(
            {
                "ok": not self.failed,
                "version": __version__,
                "docs": DOCS_URL,
                "summary": {status: counts[status] for status in STATUSES},
                "checks": [dataclasses.asdict(check) for check in self.checks],
            }
        )
        return int(ExitCode.ERROR if self.failed else ExitCode.OK)


def _transient(exc: AdopsError) -> bool:
    """A network error or a problem on the platform's side (HTTP 5xx, Meta's is_transient): not the user's setup."""
    if isinstance(exc, NetworkError):
        return True
    return isinstance(exc, ApiError) and (exc.transient or (exc.status or 0) >= 500)


def _failure_fix(exc: AdopsError, fallback: str | None = None) -> str:
    """The fix for a failed read: the network for a temporary problem, else the known error's hint or ``fallback``."""
    if _transient(exc):
        return NETWORK_HINT
    return exc.hint or fallback or GENERIC_FIX


# ---------------------------------------------------------------------- entry point
def run(
    args: argparse.Namespace,
    *,
    out: Output,
    env: Mapping[str, str],
    cwd: Path | None,
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
    runner: Callable[..., Any],
) -> int:
    report = Report(out)
    out.line(f"adops-guard {__version__} doctor: read-only checks; nothing is changed on either platform")
    report.section("Setup")
    settings = _check_config(report, args, env, cwd)
    if settings is None:
        report.add(SKIP, "platforms", "Google Ads and Meta checks are skipped until the config file loads")
        return report.finish()
    _check_state_dir(report, settings.state_dir)
    ctx = Context(settings=settings, out=out, env=env, sleep=sleep, now=now, monotonic=monotonic, runner=runner)
    only = getattr(args, "only", None)
    for platform, title, check in (("google", "Google Ads", _google), ("meta", "Meta", _meta)):
        if only and only != platform:
            continue
        report.section(title)
        explicit = only == platform
        if not explicit and not _configured(platform, settings, args, env):
            report.add(
                SKIP,
                f"{platform}.configured",
                f"{title} is not configured, so it was skipped",
                f"set it up with docs/setup.md ({title}), or run: adops-guard doctor {platform}",
            )
            continue
        try:
            check(report, ctx, args, explicit)
        except AdopsError as exc:  # e.g. a rate limit in the middle: the remaining checks of this platform wait
            report.add(FAIL, f"{platform}.stopped", f"stopped: {exc}", _failure_fix(exc))
    return report.finish()


def _configured(platform: str, settings: Settings, args: argparse.Namespace, env: Mapping[str, str]) -> bool:
    if platform == "google":
        google = settings.google
        if getattr(args, "customer_id", None) or google.customer_id or google.login_customer_id:
            return True
        if any(env.get(name) for name in GOOGLE_ENV.values()):
            return True
        return Path(google.credentials_file or env.get(GOOGLE_FILE_ENV) or GOOGLE_DEFAULT_FILE).expanduser().is_file()
    meta = settings.meta
    return bool(
        getattr(args, "ad_account", None)
        or meta.ad_account_id
        or meta.token_source
        or meta.token_file
        or env.get(meta.token_env)
        or env.get(META_FILE_ENV)
    )


# ---------------------------------------------------------------------- setup
def _check_config(
    report: Report, args: argparse.Namespace, env: Mapping[str, str], cwd: Path | None
) -> Settings | None:
    try:
        settings = load_settings(getattr(args, "config", None), env, cwd)
    except ConfigError as exc:
        report.add(FAIL, "config", str(exc), exc.hint or CONFIG_VALUE_FIX)
        return None
    if settings.source is None:
        report.add(
            WARN,
            "config",
            "no config file (./adops-guard.ini, --config or $ADOPS_GUARD_CONFIG): built-in defaults, no ceilings",
            CONFIG_FIX,
        )
    else:
        how = "found in the working directory" if settings.discovered else "named with --config or $ADOPS_GUARD_CONFIG"
        report.add(PASS, "config", f"config file {settings.source} ({how}): valid, not writable by other users")
    for name, url in settings.endpoint_overrides():
        report.add(
            WARN,
            "config.test_endpoint",
            f"{name} = {url}: a test endpoint, and credentials are sent there",
            f"remove it; {TEST_ENDPOINTS_ENV}=1 belongs in the offline test suite only",
        )
    if getattr(args, "state_dir", None):
        settings = dataclasses.replace(settings, state_dir=Path(args.state_dir).expanduser())
    return settings


def _check_state_dir(report: Report, path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        report.add(INFO, "state", f"state directory {path}: not there yet (the first write creates it, mode 0700)")
        return
    except OSError as exc:
        report.add(FAIL, "state", f"state directory {path}: {exc.strerror or exc}")
        return
    if stat.S_ISLNK(info.st_mode):
        report.add(
            FAIL,
            "state",
            f"state directory {path} is a symlink",
            "point state_dir (or --state-dir) at the real directory",
        )
        return
    if not stat.S_ISDIR(info.st_mode):
        report.add(
            FAIL, "state", f"state directory {path} exists but is not a directory", "move it, or set another state_dir"
        )
        return
    if os.name != "nt":
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            report.add(
                FAIL, "state", f"state directory {path} belongs to another user", "use a state directory you own"
            )
            return
        if info.st_mode & 0o022:
            mode = oct(info.st_mode & 0o777)
            report.add(
                FAIL,
                "state",
                f"state directory {path} can be changed by other users (mode {mode})",
                f"chmod 700 {path}",
            )
            return
    try:
        pending = StateStore(path / "state.json").load().get("pending") or {}
    except AdopsError as exc:
        report.add(FAIL, "state", str(exc), exc.hint)
        return
    if pending:
        names = sorted(pending)
        shown = ", ".join(names[:3]) + (", ..." if len(names) > 3 else "")
        report.add(
            WARN,
            "state.pending",
            f"{len(names)} earlier --apply did not finish: {shown}",
            "re-run the same command: it reads the current value first and records whether the earlier write is in "
            "place (journal.jsonl has the details)",
        )
    else:
        report.add(PASS, "state", f"state directory {path}: private, no unfinished writes")


def _check_ffprobe(report: Report, runner: Callable[..., Any]) -> None:
    try:
        result = runner(
            ["ffprobe", "-version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, check=False
        )
    except FileNotFoundError:
        report.add(INFO, "meta.ffprobe", "ffprobe not found: only meta video-check --file needs it", FFMPEG_TIP)
        return
    except (OSError, subprocess.SubprocessError) as exc:
        report.add(WARN, "meta.ffprobe", f"ffprobe did not run: {exc}", FFMPEG_TIP)
        return
    words = ((result.stdout or "").strip().splitlines() or [""])[0].split()
    if result.returncode == 0 and words[:2] == ["ffprobe", "version"] and len(words) > 2:
        report.add(PASS, "meta.ffprobe", f"ffprobe {words[2]} (for meta video-check --file)")
    else:
        report.add(WARN, "meta.ffprobe", f"ffprobe answered unexpectedly (exit {result.returncode})", FFMPEG_TIP)


# ---------------------------------------------------------------------- Google Ads
def _google(report: Report, ctx: Context, args: argparse.Namespace, explicit: bool) -> None:
    settings = ctx.settings.google
    if settings.max_daily_budget is None:
        report.add(
            WARN,
            "google.ceiling",
            "[google] max_daily_budget is not set: every budget increase and every enable will be refused",
            "add under [google] in the config file: max_daily_budget = 50.00 (your own number)",
        )
    else:
        report.add(
            PASS,
            "google.ceiling",
            f"ceiling: [google] max_daily_budget = {fmt_amount(settings.max_daily_budget)} per budget",
        )

    try:
        creds = load_google_credentials(settings, ctx.env)
    except CredentialError as exc:
        report.add(FAIL, "google.credentials", str(exc), exc.hint or "docs/setup.md, Google steps 1 to 5")
        return
    where = (
        "the GOOGLE_ADS_* environment variables"
        if creds.source == "environment"
        else f"{creds.source} (mode 0600, owned by you)"
    )
    report.add(
        PASS, "google.credentials", f"credentials from {where}: client_id, client_secret and refresh_token are set"
    )
    if creds.developer_token:
        report.add(
            INFO,
            "google.developer_token",
            "a developer token is set: optional, and ignored by Google since 9 September 2026 (access now comes "
            "from the Cloud project)",
        )

    customer = getattr(args, "customer_id", None) or settings.customer_id
    try:
        client = GoogleAdsClient(
            creds,
            customer,
            api_version=settings.api_version,
            api_base_url=settings.api_base_url,
            oauth_token_url=settings.oauth_token_url,
            login_customer_id=getattr(args, "login_customer_id", None) or settings.login_customer_id,
            sleep=ctx.sleep,
            clock=ctx.monotonic,
            allow_test_endpoints=ctx.settings.test_endpoints,
        )
    except ConfigError as exc:
        report.add(
            FAIL, "google.settings", str(exc), exc.hint or "fix [google] customer_id, login_customer_id or api_version"
        )
        return

    try:
        client.authenticate()
    except CredentialError as exc:
        report.add(FAIL, "google.oauth", str(exc), exc.hint)
        return
    except NetworkError as exc:
        report.add(FAIL, "google.oauth", str(exc), NETWORK_HINT)
        return
    report.add(PASS, "google.oauth", "OAuth: Google accepted the refresh token")

    try:
        reachable = client.list_accessible_customers()
    except (ApiError, NetworkError) as exc:
        report.add(
            FAIL, "google.access", f"listing the accounts this OAuth user can reach failed: {exc}", _failure_fix(exc)
        )
        return
    if reachable:
        shown = ", ".join(fmt_cid(c) for c in reachable[:5]) + (", ..." if len(reachable) > 5 else "")
        report.add(PASS, "google.access", f"the OAuth user reaches {len(reachable)} account(s) directly: {shown}")
    else:
        report.add(
            WARN,
            "google.access",
            "the OAuth user reaches no account directly",
            "give this Google account access (Google Ads > Admin > Access and security), or reach the account "
            "through a manager account (login_customer_id)",
        )

    if not client.customer_id:
        report.add(
            FAIL if explicit else WARN,
            "google.customer_id",
            "no customer_id: the other google commands need one",
            "set [google] customer_id = 123-456-7890 (adops-guard google accounts lists yours), or pass --customer-id",
        )
        return
    label = f"account {fmt_cid(client.customer_id)}"
    try:
        info = customer_info(client)
    except (ApiError, NetworkError) as exc:
        report.add(
            FAIL,
            "google.account",
            f"{label}: {exc}",
            _failure_fix(exc, "check customer_id and login_customer_id (adops-guard google accounts)"),
        )
        return
    if not info:
        report.add(FAIL, "google.account", f"{label}: the API returned no account data", "check customer_id")
        return
    label += f' "{info.get("descriptiveName", "")}"'
    if info.get("manager"):
        report.add(
            FAIL,
            "google.account",
            f"{label} is a manager (MCC) account: reports and changes need a client account",
            "set customer_id to a client account and login_customer_id to this manager (adops-guard google accounts)",
        )
        return
    status = info.get("status") or "UNKNOWN"
    if status != "ENABLED":
        report.add(
            FAIL,
            "google.account",
            f"{label} is {status}",
            "finish setup or reactivate it in the Google Ads UI, or use another customer_id",
        )
        return
    test_account = bool(info.get("testAccount"))
    through = f", through manager {fmt_cid(client.login_customer_id)}" if client.login_customer_id else ""
    report.add(
        PASS,
        "google.account",
        f"{label}: ENABLED, {info.get('currencyCode')}, {info.get('timeZone')}"
        + (" (test account)" if test_account else "")
        + through,
    )
    if test_account:
        report.add(INFO, "google.access_level", "a test account: every API access level reaches it")
    else:
        report.add(
            PASS,
            "google.access_level",
            "API access level: Explorer or higher (a production account answered)",
            ACCESS_LEVEL_TIP,
        )

    _google_account_budget(report, ctx, client, info)

    rows, error = try_search(client, AUTO_APPLY_QUERY)
    if rows is None:
        report.add(
            MANUAL,
            "google.auto_apply",
            f"auto-apply settings are not readable through the API here ({error})",
            AUTO_APPLY_CHECK,
        )
    else:
        enabled = sorted(
            {
                str((row.get("recommendationSubscription") or {}).get("type") or "?")
                for row in rows
                if (row.get("recommendationSubscription") or {}).get("status") == "ENABLED"
            }
        )
        if enabled:
            shown = ", ".join(enabled[:6]) + (", ..." if len(enabled) > 6 else "")
            report.add(
                WARN,
                "google.auto_apply",
                f"auto-apply is on for {len(enabled)} recommendation type(s): {shown}",
                AUTO_APPLY_FIX,
            )
        else:
            report.add(PASS, "google.auto_apply", "auto-apply: the API reports no auto-applied recommendation types")

    rows, error = try_search(client, CONVERSION_QUERY)
    if rows is None:
        report.add(
            MANUAL,
            "google.conversions",
            f"conversion actions are not readable ({error})",
            "Google Ads > Goals > Conversions > Summary",
        )
    elif not rows:
        report.add(
            INFO,
            "google.conversions",
            "no conversion actions yet (optional; an iOS app without an SDK can count App Store clicks on a "
            "landing page)",
            "adops-guard google conversion-action create --name 'Website: App Store button' "
            "(docs/setup.md, Google step 7)",
        )
    else:
        primary = [row for row in rows if (row.get("conversionAction") or {}).get("primaryForGoal")]
        if primary:
            report.add(
                PASS,
                "google.conversions",
                f"conversion goals: {len(primary)} of {len(rows)} enabled conversion action(s) are primary",
            )
        else:
            report.add(
                WARN,
                "google.conversions",
                f"{len(rows)} enabled conversion action(s), none primary: a goal whose only actions are secondary "
                "shows as 'Misconfigured', and bidding ignores it",
                CONVERSION_PRIMARY_FIX,
            )


def _google_account_budget(report: Report, ctx: Context, client: GoogleAdsClient, info: dict[str, Any]) -> None:
    currency = info.get("currencyCode")
    local_now, _ = account_now(info.get("timeZone"), ctx.now())
    try:
        rows, error = account_budget_rows(client)
        if rows is None:
            report.add(
                WARN,
                "google.account_budget",
                f"account budgets are not readable: {error}",
                "the OAuth user's role may not include billing data (Admin access does); spend-watch then needs "
                "--since",
            )
            return
        found = active_account_budget(rows, local_now)
        if not found:
            report.add(
                INFO,
                "google.account_budget",
                "no active account budget (normal with automatic payments): nothing caps total spend on Google's "
                "side, and spend-watch needs --since",
            )
            return
        start, budget = found
        billing = from_micros(budget.get("amountServedMicros"))
        metrics = metrics_spend(client, start.date(), local_now.date())
    except (ApiError, NetworkError) as exc:
        report.add(WARN, "google.account_budget", f"could not read spend: {exc}", _failure_fix(exc))
        return
    spend = max(billing, metrics)
    limit_micros = budget.get("approvedSpendingLimitMicros")
    if not limit_micros:
        kind = budget.get("approvedSpendingLimitType") or "INFINITE"
        report.add(
            PASS,
            "google.account_budget",
            f"account budget since {start.date()}: no fixed limit ({kind}); {fmt_amount(spend, currency)} spent",
        )
        return
    limit = from_micros(limit_micros)
    left = limit - spend
    text = (
        f"{fmt_amount(spend, currency)} of {fmt_amount(limit, currency)} used since {start.date()} (the larger of "
        f"billing {fmt_amount(billing)} and metrics {fmt_amount(metrics)}; billing can lag for hours)"
    )
    if left <= 0:
        report.add(FAIL, "google.account_budget", f"account budget used up: {text}", ACCOUNT_BUDGET_FIX)
    elif left * 10 < limit:
        report.add(
            WARN,
            "google.account_budget",
            f"account budget almost used: {text}, {fmt_amount(left, currency)} left",
            ACCOUNT_BUDGET_FIX,
        )
    else:
        report.add(PASS, "google.account_budget", f"account budget: {text}, {fmt_amount(left, currency)} left")


# ---------------------------------------------------------------------- Meta
def _token_source(settings: MetaSettings, env: Mapping[str, str]) -> str:
    source = settings.token_source or ("env" if env.get(settings.token_env) else "file")
    if source == "env":
        return f"${settings.token_env}"
    if source == "file":
        return f"{settings.token_file or env.get(META_FILE_ENV)} (mode 0600, owned by you)"
    if source == "keychain":
        return f"the macOS Keychain (service {settings.keychain_service})"
    return f"AWS SSM ({settings.ssm_parameter})"


def _refused_token_fix(settings: MetaSettings, token: str, exc: ApiError) -> str | None:
    """A sharper fix when Meta refuses the token itself (code 190)."""
    if _int(exc.code) != 190:
        return None
    if len(token) == 128:
        service = settings.keychain_service or "adops-guard-meta"
        return (
            "the token is exactly 128 characters: the macOS Keychain prompt cuts tokens there. Store it again "
            f'with the value on the command line: security add-generic-password -U -a "$USER" -s {service} '
            '-w "$(pbpaste)" (docs/setup.md, Meta step 6)'
        )
    if not token.startswith("EAA"):
        return (
            "this does not look like a Facebook Login token (those start with EAA): generate one in Graph API "
            "Explorer (docs/setup.md, Meta step 4)"
        )
    return None


def _meta(report: Report, ctx: Context, args: argparse.Namespace, explicit: bool) -> None:
    settings = ctx.settings.meta
    ceilings = (("max_daily_budget", settings.max_daily_budget), ("max_lifetime_budget", settings.max_lifetime_budget),
                ("max_spend_cap", settings.max_spend_cap))  # fmt: skip
    missing = [name for name, value in ceilings if value is None]
    if missing:
        report.add(
            WARN,
            "meta.ceilings",
            f"[meta] {', '.join(missing)} not set: increases of those will be refused",
            "add them under [meta] in the config file, e.g. max_daily_budget = 50.00 (your own numbers)",
        )
    else:
        shown = ", ".join(f"{name} = {fmt_amount(value)}" for name, value in ceilings)
        report.add(PASS, "meta.ceilings", f"ceilings: [meta] {shown}")
    _check_ffprobe(report, ctx.runner)

    try:
        token = load_meta_token(settings, ctx.env, ctx.runner)
    except CredentialError as exc:
        report.add(FAIL, "meta.token", str(exc), exc.hint or "docs/setup.md, Meta steps 1 to 6")
        return
    raw = token.reveal()  # only its length and its first three letters are ever looked at
    report.add(
        PASS,
        "meta.token",
        f"access token from {_token_source(settings, ctx.env)}: {len(raw)} characters (never printed)",
    )
    try:
        client = MetaClient(
            token,
            api_version=settings.api_version,
            base_url=settings.graph_base_url,
            usage_stop=settings.usage_stop_percent,
            sleep=ctx.sleep,
            allow_test_endpoints=ctx.settings.test_endpoints,
        )
    except ConfigError as exc:
        report.add(FAIL, "meta.settings", str(exc), exc.hint or "fix [meta] api_version, e.g. api_version = v26.0")
        return

    try:
        me = client.get("me", {"fields": "id,name"})
    except RateLimited:
        raise
    except (ApiError, NetworkError) as exc:
        if _transient(exc):
            report.add(FAIL, "meta.token_user", f"Meta did not answer: {exc}", NETWORK_HINT)
        elif isinstance(exc, ApiError) and _int(exc.code) in (190, 102):
            fix = _refused_token_fix(settings, raw, exc) or exc.hint or META_TOKEN_RENEW
            report.add(FAIL, "meta.token_user", f"Meta refused the token: {exc}", fix)
        else:
            report.add(FAIL, "meta.token_user", f"reading the token's user failed: {exc}", _failure_fix(exc))
        return
    report.add(PASS, "meta.token_user", f"token user: {me.get('name')} ({me.get('id')})")

    try:
        permissions = client.get("me/permissions").get("data") or []
    except RateLimited:
        raise
    except (ApiError, NetworkError) as exc:
        report.add(FAIL, "meta.permissions", f"the token's permissions are not readable: {exc}", _failure_fix(exc))
        return
    granted = {str(p.get("permission")) for p in permissions if p.get("status") == "granted"}
    refused = [f"{p.get('permission')} ({p.get('status')})" for p in permissions if p.get("status") != "granted"]
    missing = [name for name in REQUIRED_PERMISSIONS if name not in granted]
    if missing:
        report.add(
            FAIL,
            "meta.permissions",
            f"permissions missing: {', '.join(missing)}" + (f" (not granted: {', '.join(refused)})" if refused else ""),
            META_PERMISSIONS_FIX,
        )
    else:
        report.add(PASS, "meta.permissions", "permissions: ads_read and ads_management are granted")
    optional = [name for name in OPTIONAL_PERMISSIONS if name not in granted]
    if optional:
        report.add(INFO, "meta.permissions_optional", f"not granted: {', '.join(optional)}", META_OPTIONAL_TIP)

    _meta_app(report, client)

    raw_account = getattr(args, "ad_account", None) or settings.ad_account_id
    if not raw_account:
        report.add(
            FAIL if explicit else WARN,
            "meta.ad_account",
            "no ad account configured: the other meta commands need one",
            "set [meta] ad_account_id = act_123456789012345 (Ads Manager shows the id in the account menu), or pass "
            "--ad-account",
        )
        return
    try:
        account = normalize_account(raw_account)
    except ConfigError as exc:
        report.add(FAIL, "meta.ad_account", str(exc), "copy the id from Ads Manager's account menu")
        return
    try:
        info = client.get_fields(account, ACCOUNT_FIELDS)
    except RateLimited:
        raise
    except (ApiError, NetworkError) as exc:
        report.add(
            FAIL,
            "meta.ad_account",
            f"{account} is not readable with this token: {exc}",
            _failure_fix(exc, META_ACCOUNT_FIX),
        )
        return
    _meta_account(report, client, settings, account, info)
    _meta_instagram(report, client, granted)
    _meta_apps(report, client, account, getattr(args, "app", None))


def _meta_app(report: Report, client: MetaClient) -> None:
    try:
        app = client.get_fields("app", APP_FIELDS)
    except RateLimited:
        raise
    except ApiError as exc:
        report.add(
            MANUAL, "meta.app", f"the token's app is not readable through the API ({str(exc)[:160]})", META_LIVE_CHECK
        )
        return
    label = f'app "{app.get("name", "")}" ({app.get("id")})'
    dropped = set(app.get("_dropped") or [])
    missing = [what for field, what in LIVE_NEEDS if field not in dropped and not app.get(field)]
    if missing:
        report.add(
            WARN,
            "meta.app",
            f"{label} has no {', '.join(missing)}: it cannot go Live, and ad creatives made through an app in "
            "Development mode fail with 100/1885183",
            META_LIVE_FIX,
        )
    else:
        report.add(
            MANUAL,
            "meta.app_mode",
            f"{label}: whether it is in Development or Live mode is not readable through the API",
            META_LIVE_CHECK,
        )


def _meta_account(
    report: Report, client: MetaClient, settings: MetaSettings, account: str, info: dict[str, Any]
) -> None:
    status = ACCOUNT_STATUS.get(int(info.get("account_status") or 0), str(info.get("account_status")))
    currency = info.get("currency")
    label = f'ad account {account} "{info.get("name", "")}"'
    if status == "ACTIVE":
        report.add(PASS, "meta.ad_account", f"{label}: ACTIVE, {currency}, {info.get('timezone_name')}")
    else:
        reason = f" (disable reason {info.get('disable_reason')})" if info.get("disable_reason") else ""
        report.add(
            FAIL,
            "meta.ad_account",
            f"{label} is {status}{reason}",
            META_STATUS_FIX.get(status, "check Ads Manager > Account overview"),
        )

    prepaid = info.get("is_prepay_account")
    if prepaid is True:
        report.add(
            INFO,
            "meta.funding",
            "prepaid account: ads spend the funds you add; prepaid funds cannot be moved to another ad account",
        )
    elif prepaid is False:
        report.add(
            INFO,
            "meta.funding",
            "not prepaid (card or invoice): Meta charges the payment method as ads spend; funds added to another ad "
            "account do not count here",
        )

    try:
        offset = meta_offset(currency, settings.currency_offset)
    except ConfigError as exc:
        report.add(FAIL, "meta.currency", str(exc), exc.hint)
        offset = None
    if offset:

        def show(value: int) -> str:
            return fmt_amount(from_minor(value, offset), currency)

        cap = minor(info.get("spend_cap"))
        spent = minor(info.get("amount_spent")) or 0
        if not cap or cap >= NO_SPEND_CAP:
            report.add(
                WARN,
                "meta.spending_limit",
                "no account spending limit: nothing on Meta's side caps total spend (a daily budget can be overspent "
                "by up to 75% on a single day)",
                META_SPENDING_LIMIT_FIX,
            )
        else:
            left = cap - spent
            text = f"account spending limit {show(cap)}: {show(spent)} spent, {show(max(left, 0))} left"
            if left <= 0:
                report.add(
                    FAIL,
                    "meta.spending_limit",
                    f"{text}: the limit is reached, so all ads stop",
                    META_SPENDING_LIMIT_FIX,
                )
            elif left * 10 < cap:
                report.add(WARN, "meta.spending_limit", f"{text} (under 10% left)", META_SPENDING_LIMIT_FIX)
            else:
                report.add(PASS, "meta.spending_limit", text)

    header = client.usage.get("x-ad-account-usage")
    tier = header.get("ads_api_access_tier") if isinstance(header, dict) else None
    percent = client.usage_percent()
    usage = f"usage {percent:.0f}% (adops-guard stops at {client.usage_stop}%)"
    if percent >= client.usage_stop:
        report.add(
            FAIL,
            "meta.rate",
            f"Meta API {usage}",
            "wait: the usage window is rolling (up to an hour); see: adops-guard meta usage",
        )
    elif tier == "development_access":
        report.add(
            INFO,
            "meta.rate",
            f"rate tier development_access (the default for new apps, with low limits); {usage}",
            META_TIER_TIP,
        )
    elif tier:
        report.add(PASS, "meta.rate", f"rate tier {tier}; {usage}")
    else:
        report.add(INFO, "meta.rate", f"rate tier: not reported in this response; {usage}")


def _meta_instagram(report: Report, client: MetaClient, granted: set[str]) -> None:
    if "pages_show_list" not in granted:
        report.add(
            MANUAL,
            "meta.instagram",
            "Instagram identity: reading Pages and their Instagram accounts needs pages_show_list",
            META_IG_CHECK,
        )
        return
    # The Page's instagram_business_account field gives the id; IG User fields such as username need instagram_basic.
    ig_field = (
        "instagram_business_account{id,username}" if "instagram_basic" in granted else "instagram_business_account"
    )
    try:
        pages = client.get_all("me/accounts", {"fields": f"id,name,{ig_field}"}, cap=200)
    except RateLimited:
        raise
    except ApiError as exc:
        report.add(MANUAL, "meta.instagram", f"Pages are not readable ({str(exc)[:160]})", META_IG_CHECK)
        return
    linked = [
        (page, page["instagram_business_account"])
        for page in pages
        if isinstance(page.get("instagram_business_account"), dict)
    ]
    if not pages:
        report.add(
            WARN,
            "meta.instagram",
            "the token sees no Facebook Page: ads need a Page, and Instagram placements an Instagram account "
            "linked to it",
            "regenerate the token and tick the Page AND the Instagram account in the dialog "
            "(docs/setup.md, Meta step 4)",
        )
    elif not linked:
        report.add(
            WARN, "meta.instagram", f"{len(pages)} Page(s), none with a linked Instagram account", META_IG_LINK_FIX
        )
    else:
        page, account = linked[0]
        more = f" (and {len(linked) - 1} more)" if len(linked) > 1 else ""
        who = f"@{account['username']} ({account.get('id')})" if account.get("username") else str(account.get("id"))
        report.add(
            PASS,
            "meta.instagram",
            f'Instagram identity: {who} through Page "{page.get("name")}"{more}; creatives name it with '
            "instagram_user_id",
        )


def _store_ids(app: dict[str, Any]) -> set[str]:
    urls = app.get("object_store_urls")
    values = urls.values() if isinstance(urls, dict) else urls if isinstance(urls, list) else [urls]
    return {match for value in values if isinstance(value, str) for match in re.findall(r"/id(\d+)", value)}


def _meta_apps(report: Report, client: MetaClient, account: str, wanted: str | None) -> None:
    try:
        apps = client.get_all(
            f"{account}/advertisable_applications", {"fields": "id,name,object_store_urls,supported_platforms"}, cap=200
        )
    except RateLimited:
        raise
    except ApiError as exc:
        report.add(
            MANUAL,
            "meta.apps",
            f"the apps this ad account can advertise are not readable ({str(exc)[:160]})",
            "Business settings > Accounts > Apps > (app) > Connected assets must list the ad account",
        )
        return
    if wanted:
        target = wanted.strip().removeprefix("id")
        match = next((app for app in apps if str(app.get("id")) == target or target in _store_ids(app)), None)
        if match:
            report.add(
                PASS,
                "meta.apps",
                f'app {wanted} can be advertised from {account}: "{match.get("name")}" ({match.get("id")})',
            )
        else:
            report.add(
                FAIL,
                "meta.apps",
                f"app {wanted} cannot be advertised from {account}: App Promotion campaigns cannot use it",
                META_APP_PROMOTION_FIX,
            )
    elif apps:
        shown = ", ".join(f'"{app.get("name")}" ({app.get("id")})' for app in apps[:5])
        report.add(INFO, "meta.apps", f"apps this ad account can advertise: {shown}")
    else:
        report.add(
            INFO,
            "meta.apps",
            "this ad account can advertise no app (only App Promotion campaigns need one)",
            "docs/setup.md, Meta step 9",
        )
