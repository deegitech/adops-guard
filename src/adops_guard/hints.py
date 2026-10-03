"""One-line fixes for platform errors we know.

Each table maps an error that the Google Ads API, Google's OAuth endpoint or
the Meta Graph API returns to the one thing that usually fixes it: a menu path,
a permission name or a command. The CLI prints it after the error as
``error: hint: ...``, and ``adops-guard doctor`` prints it under a failed
check. ``docs/troubleshooting.md`` has the same entries with more detail (a
test keeps the two in step).

Console menus get renamed often: the paths name the usual labels, written with
``>`` between the steps. Hints are plain ASCII, because they end up in cron mail
and CI logs.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# ---------------------------------------------------------------------- Meta Graph / Marketing API
META_TOKEN_RENEW = (
    "generate a new token (Graph API Explorer > Generate Access Token, then Access Token Debugger > "
    "Extend Access Token) and store it again (docs/setup.md, 'Token expiry and renewal')"
)
META_RATE_HINT = "Meta rate limit reached: wait, then re-run (see: adops-guard meta usage)"
# The CLI points at the doctor; the doctor itself prints the hint without this tail (it would point at itself).
CHECK_WITH_DOCTOR = "; check with: adops-guard doctor meta"
META_PERMISSION_HINT = (
    "permission error: the token needs ads_read (reads) and ads_management (writes), and its user needs a role on "
    "the ad account (Business settings > Accounts > Ad accounts > Assign people)" + CHECK_WITH_DOCTOR
)
META_USE_CASE_FIX = (
    "add the use case 'Create & manage ads with Marketing API' (App dashboard > Use cases > Customize: "
    "ads_management, ads_read), then generate a new token"
)
META_RATE_CODES = frozenset({4, 17, 32, 613, *range(80000, 80015)})

# ---------------------------------------------------------------------- network (both platforms)
NETWORK_HINT = (
    "a network or service problem, not your setup: check the connection or proxy (HTTPS_PROXY), then re-run later"
)

# (code, error_subcode) pairs. Code 190 and its subcodes are documented by Meta; the rest were observed
# while running app-install and traffic campaigns (docs/meta-gotchas.md).
META_PAIR_HINTS: dict[tuple[int, int], str] = {
    (190, 458): "the user removed this app or never authorised it: " + META_TOKEN_RENEW,
    (190, 459): "the Facebook account is checkpointed: log in at facebook.com and clear the checkpoint, then "
    + META_TOKEN_RENEW,
    (190, 460): "the token stopped working after a password change: " + META_TOKEN_RENEW,
    (190, 463): "the access token expired (user tokens last about 60 days after extending): " + META_TOKEN_RENEW,
    (190, 464): "the Facebook account is not confirmed: confirm it at facebook.com, then " + META_TOKEN_RENEW,
    (190, 467): "the access token is invalid (logged out, revoked, or cut short when it was stored: store it with "
    'the -w "$(pbpaste)" form, see docs/setup.md): ' + META_TOKEN_RENEW,
    (190, 492): "the token's user no longer has a role on the Page: give the role back, then " + META_TOKEN_RENEW,
    (100, 33): "the object does not exist, or the token's user cannot see it: check the id, and give the user (or "
    "system user) access in Business settings > Accounts > Ad accounts > (account) > Assign people",
}
# Subcodes that identify the problem whatever the code is.
META_SUBCODE_HINTS: dict[int, str] = {
    1885183: "the Meta app behind this token is in Development mode: switch it to Live (App dashboard > App Mode: "
    "Live, or Publish; it needs a privacy policy URL, a category and an icon in App settings > Basic)",
    3858385: "Meta wants the account owner to pass a security check (identity verification): open Ads Manager and "
    "follow the banner, or go to facebook.com/accountquality; running ads continue, new edits wait until it is done",
    1487810: "a Traffic campaign cannot link to an App Store URL: use the App Promotion objective "
    "(OUTCOME_APP_PROMOTION, promoted_object {application_id, object_store_url}) or link to your own landing page",
    2446880: "Meta asked for a WhatsApp number: link one to the Facebook Page (Page settings > Linked accounts > "
    "WhatsApp) or to the Instagram account the ads use (docs/meta-gotchas.md)",
    3858504: "standard_enhancements can no longer be set on a creative: set each feature in "
    "degrees_of_freedom_spec.creative_features_spec instead",
    4834011: "set is_adset_budget_sharing_enabled (true or false) on the campaign when the budgets live on the ad sets",
    2490589: "the Instagram 'explore' position is gone: use 'explore_home' in targeting.instagram_positions",
}
META_CODE_HINTS: dict[int, str] = {
    190: "the access token is invalid or expired: " + META_TOKEN_RENEW,
    102: "the session behind the token is no longer valid: " + META_TOKEN_RENEW,
    10: "the app or token lacks a permission: " + META_USE_CASE_FIX,
    3: "the app lacks the capability for this call (no Marketing API use case): " + META_USE_CASE_FIX,
    270: "the app is on the lowest Marketing API access tier (development access): the token's user (or system "
    "user) must be an admin of both the app and the ad account, then generate a new token; or upgrade the "
    "'Marketing API Access Tier' feature (older consoles: 'Ads Management Standard Access') in App dashboard > "
    "App Review > Permissions and features",
    368: "Meta blocked this action for a while (policy): wait, and check facebook.com/accountquality",
    2635: "this Graph API version is retired: set [meta] api_version to a current version (see Meta's changelog)",
    1: "an unknown error on Meta's side: re-run later (reads are retried; writes are never retried blindly)",
    2: "Meta's service is temporarily unavailable: re-run later (reads are retried; writes are never retried blindly)",
}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def meta_hint(code: Any, subcode: Any = None) -> str | None:
    """The fix for a Graph API error, from its ``code`` and ``error_subcode`` (None if we know none)."""
    number, sub = _int(code), _int(subcode)
    if number is not None and sub is not None and (number, sub) in META_PAIR_HINTS:
        return META_PAIR_HINTS[(number, sub)]
    if sub is not None and sub in META_SUBCODE_HINTS:
        return META_SUBCODE_HINTS[sub]
    if number is None:
        return None
    if number in META_RATE_CODES:
        return META_RATE_HINT
    if number in META_CODE_HINTS:
        return META_CODE_HINTS[number]
    if 200 <= number <= 299:
        return META_PERMISSION_HINT
    return None


# ---------------------------------------------------------------------- Google Ads API
GOOGLE_NEW_REFRESH_TOKEN = (
    "create a new refresh token and copy it into google-ads.yaml (docs/setup.md, Google steps 4 and 5)"
)
GOOGLE_QUOTA_HINT = (
    "the daily operations quota of your API access level is used up (Explorer: 2,880 a day on production "
    "accounts): retry after the 24-hour window, or apply for Basic access (Cloud console > Google Ads API > "
    "Overview > Upgrade access level)"
)
GOOGLE_VERSION_HINT = (
    "HTTP 404 from the Google Ads API: this API version may be retired; set [google] api_version = auto, or a newer vNN"
)
# Error codes (the values of errorCode in a GoogleAdsFailure) and google.rpc.ErrorInfo reasons.
GOOGLE_HINTS: dict[str, str] = {
    "USER_PERMISSION_DENIED": "the OAuth user cannot reach this account directly: set login_customer_id to the "
    "manager (MCC) account you reach it through, or give the user access (Google Ads > Admin > Access and security)",
    "INVALID_LOGIN_CUSTOMER_ID_SERVING_CUSTOMER_ID_COMBINATION": "login_customer_id is not a manager of "
    "customer_id: set it to the manager account the client account is linked to, or remove it",
    "CUSTOMER_NOT_ENABLED": "the account is not enabled (cancelled, suspended or setup not finished): finish setup "
    "and billing in the Google Ads UI, or use another customer_id",
    "CUSTOMER_NOT_FOUND": "no account has this customer_id: check the 10 digits (shown next to the account name at "
    "the top of the Google Ads UI)",
    "DEVELOPER_TOKEN_NOT_APPROVED": "your Cloud project's API access level does not allow this: Test reaches test "
    "accounts only (apply for Explorer); Explorer excludes Keyword Planner, audience insights, reach planning and "
    "billing services (apply for Basic). Cloud console > Google Ads API > Overview > Upgrade access level",
    "NOT_ADS_USER": "the Google account behind the refresh token has no Google Ads access: sign in to "
    "ads.google.com with it once, or create a new refresh token as a user who has access and copy it into "
    "google-ads.yaml (docs/setup.md, Google steps 4 and 5)",
    "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION": "the Google Cloud project that owns your OAuth client still has "
    "Test access (test accounts only): Cloud console > Google Ads API > Overview > Upgrade access level > Apply for "
    "access (Explorer; Google may grant it right away) (docs/setup.md, Google step 2)",
    "ACTION_NOT_PERMITTED": "the OAuth user's role in this Google Ads account is too low for this (Read only "
    "cannot change anything): Google Ads > Admin > Access and security > Standard or Admin; on API versions "
    "before v25 it is also what a Cloud project with Test access gets on a production account (Cloud console > "
    "Google Ads API > Overview > Upgrade access level)",
    "TWO_STEP_VERIFICATION_NOT_ENROLLED": "turn on 2-Step Verification for the Google account behind the refresh "
    "token (myaccount.google.com > Security), then retry",
    "OAUTH_TOKEN_REVOKED": "the OAuth grant was revoked: " + GOOGLE_NEW_REFRESH_TOKEN,
    "OAUTH_TOKEN_DISABLED": "the OAuth grant was disabled: " + GOOGLE_NEW_REFRESH_TOKEN,
    "OAUTH_TOKEN_INVALID": "Google refused the access token: " + GOOGLE_NEW_REFRESH_TOKEN,
    "RESOURCE_EXHAUSTED": GOOGLE_QUOTA_HINT,
    "RESOURCE_TEMPORARILY_EXHAUSTED": "too many requests in a short time: wait a few minutes, then re-run",
    "UNRECOGNIZED_FIELD": "this field may not exist in the configured API version: set [google] api_version (or auto)",
    "PROHIBITED_FIELD_IN_SELECT_CLAUSE": "this field cannot be selected from this resource",
    "SERVICE_DISABLED": "the Google Ads API is not enabled in the Cloud project of your OAuth client: Cloud "
    "console > APIs & Services > Library > Google Ads API > Enable",
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": "the refresh token lacks the https://www.googleapis.com/auth/adwords scope: "
    + GOOGLE_NEW_REFRESH_TOKEN,
    "ONE_WEBSITE_PER_AD_GROUP": "an ad group cannot mix apps.apple.com and your own website as final URLs: put the "
    "website ads in an ad group of their own",
}


def google_hint(codes: Iterable[str], status: int | None = None) -> str | None:
    """The fix for a Google Ads API error, from its error codes (first known one wins) and HTTP status."""
    for code in codes:
        if code in GOOGLE_HINTS:
            return GOOGLE_HINTS[code]
    if status == 429:
        return GOOGLE_QUOTA_HINT
    if status == 404:
        return GOOGLE_VERSION_HINT
    return None


# ---------------------------------------------------------------------- Google OAuth token endpoint
OAUTH_DEFAULT_HINT = "check client_id, client_secret and refresh_token (they must come from the same OAuth client)"
OAUTH_HINTS: dict[str, str] = {
    "invalid_grant": "the refresh token was revoked or has expired; refresh tokens of OAuth apps in 'Testing' "
    "status expire after 7 days: publish the app (Google Auth Platform > Audience > Publish app; older consoles: "
    "OAuth consent screen), then " + GOOGLE_NEW_REFRESH_TOKEN,
    "invalid_client": "client_id or client_secret is wrong, or they come from different OAuth clients: copy both "
    "again from the same Desktop-app client (Google Auth Platform > Clients, or APIs & Services > Credentials)",
    "unauthorized_client": "the refresh token was made with a different OAuth client: create a new one with this "
    "client_id and client_secret and copy it into google-ads.yaml (docs/setup.md, Google steps 4 and 5)",
}


def oauth_hint(error: Any) -> str:
    """The fix for a refusal from Google's OAuth token endpoint (``error`` is its ``error`` field)."""
    return OAUTH_HINTS.get(str(error or ""), OAUTH_DEFAULT_HINT)
