<h1 align="center">adops-guard</h1>

<p align="center">
  <strong>Guard-railed command-line ad operations for small teams on the Google Ads API and the Meta Marketing API,<br>
  built so you can't accidentally spend money.</strong>
</p>

<p align="center">
  <a href="https://github.com/deegitech/adops-guard/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/deegitech/adops-guard/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/deegitech/adops-guard/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/deegitech/adops-guard/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.10 to 3.14" src="https://img.shields.io/badge/python-3.10%E2%80%933.14-3776AB">
  <img alt="Runtime dependencies: none" src="https://img.shields.io/badge/runtime%20dependencies-none-brightgreen">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

<p align="center"><a href="README.tr.md">Türkçe özet</a></p>

---

```text
$ adops-guard google budget set --campaign 11111111111 --amount 80
error: the daily budget 80.00 USD is above the configured ceiling 50.00 USD ([google] max_daily_budget)
error: hint: lower the amount, raise the ceiling in the config file, or pass --override-limit

$ adops-guard google budget set --campaign 11111111111 --amount 25
PLAN     google budget.set  customers/1234567890/campaignBudgets/911111111111
         Campaign 11111111111 "Brand": daily budget 30.00 USD -> 25.00 USD
CHECK    validated by the API (validate only); nothing was written
DRY RUN  nothing changed. Re-run with --apply to make this change.

$ adops-guard google budget set --campaign 11111111111 --amount 25 --apply
PLAN     google budget.set  customers/1234567890/campaignBudgets/911111111111
         Campaign 11111111111 "Brand": daily budget 30.00 USD -> 25.00 USD
CHECK    validated by the API (validate only); nothing was written
APPLY    sent
VERIFY   read back 25.00 USD: OK
```

Every write is a **dry run** until you pass `--apply`. Before a write, the platform's own validator
checks the exact request; after it, the change is **read back**, and every step is **journaled**.
Spend increases above the ceilings in your config file are refused. Credentials never touch argv,
URLs, logs or state files.

## First 15 minutes

1. **Install** (Python 3.10 or newer): `pipx install "git+https://github.com/deegitech/adops-guard.git@v0.1.0"`.
2. **Set up the platform side** with [docs/setup.md](docs/setup.md): every console click for the
   Google Cloud project, the OAuth client and the refresh token, and for the Meta app, its
   permissions, Live mode and a token stored safely (macOS Keychain, a 0600 file, the environment or
   AWS SSM).
3. **Check it:** `adops-guard doctor` runs the read-only checks in order and marks each one ✓ or ✗,
   with the exact fix (a menu path, a permission name or a command) under every ✗. A platform you
   have not set up yet is skipped. It never prints a secret, and exits 0 only when nothing failed.
4. **First dry run:** `adops-guard google status` or `adops-guard meta status`, then any write
   without `--apply`.
5. **Stuck?** [docs/troubleshooting.md](docs/troubleshooting.md) lists every error code and message
   with its meaning and its fix. Commands print the same fix after an error as `error: hint: ...`.

## Contents

- [First 15 minutes](#first-15-minutes)
- [Why](#why)
- [Features](#features)
- [Install](#install)
- [Quickstart](#quickstart)
- [Commands](#commands)
- [How a write works](#how-a-write-works)
- [Configuration reference](#configuration-reference)
- [Credentials](#credentials)
- [Examples](#examples)
- [Security model](#security-model)
- [Exit codes](#exit-codes)
- [Limitations and honest caveats](#limitations-and-honest-caveats)
- [FAQ](#faq)
- [Development](#development)
- [About](#about--built-by-deegitech)
- [License](#license)

## Why

Ad platforms make it easy to spend money and hard to undo it. A typo in a budget, a script run twice,
a campaign enabled in the wrong account, or a watch script that misses a billing counter that lags for
hours: each of these costs real money before anyone notices.

Small teams usually end up with one of two things: clicking around the web UIs, which leaves no
record, or ad-hoc scripts around the official client libraries, which act immediately. adops-guard
sits in between. It is a small, auditable CLI for the handful of operations a small team does every
day (check status, read a report, change a budget, pause or enable, watch spend), and it treats every
write as something that has to be planned, validated, confirmed and verified.

It is extracted from the scripts a small game studio used to run its own Google Ads and Meta
campaigns, rewritten and generalised for anyone to use.

## Features

**Shared**

- Plan, platform-side validation (`validateOnly` / `execution_options=["validate_only"]`), `--apply`,
  read-back, journal. The same path for every write. A dry run that sends a validate-only request
  reads the object before and after it, and stops if anything changed.
- Spend ceilings per platform: raising a budget or a cap above the ceiling, removing a cap, or enabling
  something whose budget is above the ceiling is refused; with no ceiling configured, every increase
  is refused.
- Idempotent: the current value is read before every write, a repeated command is a no-op, and a
  single-writer lock stops two writes from running at once.
- Zero runtime dependencies: Python 3.10+ standard library only (`urllib`, `configparser`, `json`).
- Human output in plain ASCII for cron, systemd and CI logs; `--json` for scripts (exactly one JSON
  document on stdout, errors included).
- `doctor`: read-only checks of the whole setup (config, credentials and their file modes, OAuth,
  account access, account budget and spending-limit headroom, auto-apply, conversion goals, Meta
  permissions, app, ad account status, prepaid or card, rate tier, Instagram identity, advertisable
  apps), each failure with its fix. Known API errors get a one-line fix in every command
  ([troubleshooting](docs/troubleshooting.md)).

**Google Ads** (REST, no client library, API v25 by default)

- `status`, `report` (by day or campaign), `accounts`.
- `budget set`, `pause`, `enable` for campaigns, ad groups and ads.
- `channel-controls`: turn YouTube in-stream / in-feed / Shorts, Discover, Gmail and Display on or off
  for Demand Gen ad groups, with one leaf field mask per channel so nothing else is touched.
- `audit`: a click-quality audit with breakdowns by channel/ad format, placement (YouTube channels and
  videos), hour and age. It flags in-stream CTR far above Shorts/in-feed, TV-drama and kids channels,
  a high night-time share, and CTR rising while CPC falls. A heuristic, and it says so.
- `spend-watch`: polls spend using the **larger** of the billing counter and real-time metrics (the
  billing counter can lag for hours), and at a target pauses campaigns and lowers budgets, once. It
  counts from the account budget's start or from `--since`, never all-time spend.
- `conversion-action create` / `list`: a `WEBPAGE` conversion action (outbound click by default) and
  the `send_to` value for your landing page.

**Meta Marketing API** (Graph API v26.0 by default)

- `whoami`, `status`, `report`, `usage`.
- `set status` (campaigns, ad sets and ads) and `set budget` (daily or lifetime, campaigns and ad
  sets), with read-back. A dry run sends Meta nothing unless you add `--validate`.
- `spend-cap`: set, lower or remove a campaign spend cap.
- A usage guard that reads Meta's rate-limit headers and stops at 85 % (configurable).
- `video-check`: `is_instagram_eligible` for uploaded videos and an `ffprobe` length check for local
  files (observed in October 2026: cuts of about 17 to 19 s came back ineligible for Instagram
  placements, a 14.8 s cut eligible; 15 s or longer is flagged).
- Writes are limited to objects in the configured ad account.
- [`docs/meta-gotchas.md`](docs/meta-gotchas.md): errors we hit and what fixed them, each with the
  month we saw it.

**Landing page**

- [`snippets/landing-consent-conversion.html`](snippets/landing-consent-conversion.html): Google Consent
  Mode v2 (denied by default in the EEA, the UK and Switzerland, with a banner), a conversion on App
  Store button clicks, and `?ct=...` rewritten into App Store Connect campaign links.

## Install

Python 3.10 or newer. No other dependencies.

```bash
pipx install "git+https://github.com/deegitech/adops-guard.git@v0.1.0"   # a reviewed release
# or the latest main:
pipx install git+https://github.com/deegitech/adops-guard.git
# or, in a virtual environment:
pip install "git+https://github.com/deegitech/adops-guard.git@v0.1.0"

adops-guard --version
```

For anything that runs unattended (cron, systemd, launchd), pin a release tag as above, read its
changelog, and upgrade on purpose. From a checkout: `pip install .` (or `pip install -e ".[dev]"` to
work on it).

## Quickstart

```bash
mkdir -p ~/adops && cd ~/adops
curl -fsSLO https://raw.githubusercontent.com/deegitech/adops-guard/v0.1.0/examples/adops-guard.example.ini
mv adops-guard.example.ini adops-guard.ini
chmod go-w adops-guard.ini                       # a config that other users can change is refused
# then edit it: uncomment and set customer_id and ad_account_id, and set your own ceilings
```

**Google Ads.** Put your OAuth client id, client secret and refresh token in a private
`~/google-ads.yaml` ([example](examples/google-ads.example.yaml); see
[Getting Google Ads credentials](#getting-google-ads-credentials)). Google's own client libraries
read the same file, so if you already have one, add the three keys to it instead of replacing it:

```bash
if [ -e ~/google-ads.yaml ]; then
  echo "~/google-ads.yaml exists: add client_id, client_secret and refresh_token to it"
else
  (umask 077 && curl -fsSL -o ~/google-ads.yaml \
    https://raw.githubusercontent.com/deegitech/adops-guard/v0.1.0/examples/google-ads.example.yaml)
fi
chmod 600 ~/google-ads.yaml            # then replace the REPLACE_ME values
adops-guard google accounts            # which accounts can this OAuth user reach?
adops-guard google status              # account, account budgets, campaigns, last 30 days
adops-guard google report --by campaign --days 7
adops-guard google audit --days 14     # click-quality audit
```

**Meta.** Give the token through the environment (or a file, the Keychain or SSM, see
[Credentials](#credentials)):

```bash
read -rs META_ACCESS_TOKEN && export META_ACCESS_TOKEN   # paste, Enter: not echoed, not in history
adops-guard meta whoami
adops-guard meta status
adops-guard meta report --level campaign --by-day
```

Every write command works the same way: run it once to see the plan (Google commands also have the
API validate it; for Meta add `--validate`), then run it again with `--apply`.

Not sure everything is in place? `adops-guard doctor` (or `doctor google`, `doctor meta`) checks it
and prints the fix for each problem; [docs/setup.md](docs/setup.md) walks through every console step.

## Commands

| Command | What it does | Writes? |
|---|---|---|
| `google accounts` | Accounts the OAuth user can reach, and the accounts under a manager account | no |
| `google status` | Account, account budgets (invoicing), campaigns with budgets and 30-day cost | no |
| `google report [--by day\|campaign] [--days N \| --from --to] [--campaign ID]` | Impressions, clicks, CTR, CPC, cost, conversions | no |
| `google audit [--campaign ID] [--days N] [--fail-on-signal]` | Click-quality audit ([docs](docs/click-quality-audit.md)) | no |
| `google budget set --campaign ID --amount X [--shared-ok]` | Set a campaign's daily budget | yes |
| `google pause --campaign ID \| --ad-group ID \| --ad AG~AD` | Pause | yes |
| `google enable --campaign ID \| --ad-group ID \| --ad AG~AD` | Enable (checked against the ceiling) | yes |
| `google channel-controls --ad-group ID [--youtube-in-stream on\|off] ...` | Demand Gen channels; without options it shows them | yes |
| `google spend-watch --target X [--then-budget ID=X] [--then-pause ID]` | Poll spend; act once at the target | yes, at the target |
| `google conversion-action list` / `create --name N` | Website conversion actions | create: yes |
| `meta whoami` | Token user, granted permissions, ad account | no |
| `meta status [--campaign ID]` | Ad account, campaigns (and one campaign's ad sets and ads) | no |
| `meta report [--level account\|campaign\|adset\|ad] [--by-day]` | Spend, impressions, reach, clicks, CTR, CPC | no |
| `meta usage` | Rate-limit usage from Meta's headers | no |
| `meta video-check [VIDEO_ID ...] [--file PATH ...]` | Instagram eligibility of ad videos | no |
| `meta set status ID ACTIVE\|PAUSED [--type ...]` | Status of a campaign, ad set or ad | yes |
| `meta set budget ID --daily X \| --lifetime X` | Budget of a campaign or ad set | yes |
| `meta spend-cap CAMPAIGN_ID --amount X \| --remove` | Campaign spend cap | yes |
| `doctor [google\|meta] [--app ID]` | Read-only checks of the setup, each failure with its fix ([setup](docs/setup.md)) | no |

Every write takes `--apply` (default: dry run). Spend-increasing writes also take `--override-limit`
to skip the ceiling for that one command, and Meta writes take `--validate` (see below). Global
options: `--config PATH`, `--state-dir DIR`, `--json`, `--debug`, and `--customer-id` /
`--login-customer-id` (Google) or `--ad-account` (Meta). `adops-guard <platform> <command> --help`
describes each command.

## How a write works

```text
 read the current value from the API ------------> already the target value? "NO-OP", exit 0
        |
 guards: ceilings, shared budgets, account scope --> refused? exit 3, nothing written
        |
 plan: print "old -> new" and any notes
        |
 no --apply? validate-only request (Google always, Meta with --validate), with the object
        |    read before and after it: changed? journal "dry-run-wrote", exit 4
        |    then "DRY RUN", exit 0
        |
 --apply: validate-only request, then take the single-writer lock
          journal "intent"  ->  send the write  ->  journal "applied" (or "error" / "ambiguous")
          read the object back  ->  journal "verified" (exit 0) or "mismatch" (exit 4)
```

A write that fails ambiguously (a timeout or an HTTP 5xx) is never retried automatically. The journal
keeps an `ambiguous` entry and the state file a `pending` marker. Running the same command again reads
the current value first and only writes if it is still needed; if the earlier write turns out to be
in place, the journal gets a `verified` line for it and the marker is cleared.

**Why Meta dry runs need `--validate`.** Meta documents `execution_options=["validate_only"]` for
updates, but this version of adops-guard has only been run against fake APIs, and we used Meta's
validate-only mode live only for creating objects. If Meta ever applied such a request, a dry run of
`meta set status ... ACTIVE` would start spending. So a Meta dry run sends nothing by default;
`--validate` sends the validate-only request (with the before/after check above), and `--apply`
always sends it before the real write.

## Configuration reference

The config file is INI. It is looked up as `--config PATH`, then `$ADOPS_GUARD_CONFIG`, then
`./adops-guard.ini` (a file found that way is announced on stderr: `using config ...`). Everything is
optional; unknown keys are errors (a misspelled ceiling must not be ignored silently), and keys that
look like secrets are refused. A config file that other users can write to, or that belongs to another
user (root excepted), is refused: it decides your ceilings and where tokens come from.
See [`examples/adops-guard.example.ini`](examples/adops-guard.example.ini).

| Section | Key | Default | Meaning |
|---|---|---|---|
| `general` | `state_dir` | `.adops-guard` | Journal, state and lock (relative to the config file). Also `$ADOPS_GUARD_STATE_DIR` or `--state-dir`. |
| `google` | `customer_id` | - | The account to work on (`123-456-7890`). Or `--customer-id`. |
| `google` | `login_customer_id` | - | Manager (MCC) account, if you reach the account through one. |
| `google` | `credentials_file` | `~/google-ads.yaml` | Order: this key, then `$GOOGLE_ADS_CONFIGURATION_FILE_PATH`, then `~/google-ads.yaml`. Must be chmod 600. |
| `google` | `api_version` | `v25` | Google Ads API version, or `auto`. |
| `google` | `max_daily_budget` | none | Ceiling for each daily budget you raise, and for the budget of anything you enable. None = every increase refused. |
| `meta` | `ad_account_id` | - | `act_...`. Writes are limited to objects in this account. Or `--ad-account`. |
| `meta` | `api_version` | `v26.0` | Graph API version. |
| `meta` | `token_source` | env, then file | `env`, `file`, `keychain` or `ssm`. |
| `meta` | `token_env` | `META_ACCESS_TOKEN` | Environment variable for `token_source = env`. |
| `meta` | `token_file` | - | chmod 600 file for `token_source = file` (or `$META_ACCESS_TOKEN_FILE`). |
| `meta` | `keychain_service`, `keychain_account` | - | macOS Keychain item for `token_source = keychain`. |
| `meta` | `ssm_parameter`, `ssm_region` | - | SSM SecureString for `token_source = ssm`. |
| `meta` | `usage_stop_percent` | `85` | Stop sending requests at this rate-limit usage. |
| `meta` | `max_daily_budget` | none | Ceiling for each daily budget you raise or enable with (a campaign with ad set budgets: the sum of its active ad sets). |
| `meta` | `max_lifetime_budget` | none | Ceiling for lifetime budgets. |
| `meta` | `max_spend_cap` | none | Ceiling for raising a campaign spend cap. |
| `meta` | `currency_offset` | built in | Minor units per unit, for currencies missing from the built-in table. |
| `audit` | `min_clicks`, `min_impressions`, `instream_ctr_ratio`, `night_hours`, `night_click_share`, `night_ctr_ratio`, `flagged_placement_share`, `ctr_rise`, `cpc_fall`, `min_days`, `age_undetermined_share`, `extra_kids_keywords`, `extra_drama_keywords` | see [docs](docs/click-quality-audit.md) | Audit thresholds and extra keywords. |

**Ceilings are per budget, not per account.** They apply to each budget you change or enable (for a
Meta campaign whose budgets live on its ad sets: the sum of its active ad sets), not to the account
total: ten campaigns can each be raised to the ceiling. Use the platforms' account-level limits for a
total (a Google Ads account budget, the Meta account spending limit).

Three more keys exist only for the offline test suite: `google.api_base_url`, `google.oauth_token_url`
and `meta.graph_base_url`. Credentials are sent to them, so they are refused unless the environment
variable `ADOPS_GUARD_TEST_ENDPOINTS=1` is set, and even then they accept only the official API host
or a loopback address, with a warning on every run.

## Credentials

**Google Ads** uses the same keys as Google's client libraries: `client_id`, `client_secret`,
`refresh_token`, and optionally `login_customer_id` and `developer_token`.

- Environment: `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_REFRESH_TOKEN` (all
  three, or none), and optionally `GOOGLE_ADS_LOGIN_CUSTOMER_ID` and `GOOGLE_ADS_DEVELOPER_TOKEN`.
  When they are set, they win over any file.
- Or a `google-ads.yaml` (flat `key: value` lines) that only you can read: `chmod 600`. Files other
  users can read, or owned by someone else, are refused. Which file: `credentials_file` in the config,
  else `$GOOGLE_ADS_CONFIGURATION_FILE_PATH`, else `~/google-ads.yaml`.

A developer token is optional. Google sunset developer tokens on 9 September 2026: API access now
belongs to your Google Cloud project, the `developer-token` header is ignored, and Google says it will
reject it in a future major API version ([Google's note](https://developers.google.com/google-ads/api/docs/get-started/dev-token)).
adops-guard sends one only if you configure it.

### Getting Google Ads credentials

Current as of October 2026; Google changes this flow from time to time, so follow its pages if they
differ. [docs/setup.md](docs/setup.md#google-ads) has every click:

1. In the Google Cloud Console, create (or pick) a project and enable the **Google Ads API**.
2. Open the project's [Google Ads API Overview page](https://console.cloud.google.com/google/ads-apis/overview).
   New projects get **Test** access (test accounts only); apply for **Explorer** access (or higher) to
   reach real accounts. See [access levels](https://developers.google.com/google-ads/api/docs/api-policy/access-levels)
   for what each level reaches and its daily operations quota.
3. Configure the OAuth consent screen with the `https://www.googleapis.com/auth/adwords` scope, create
   an OAuth client of type **Desktop app**, and publish the app: while the consent screen stays in
   *Testing* status, the refresh token expires after 7 days.
4. Generate a refresh token for a Google user who can access your Google Ads accounts, with Google's
   [single-user authentication workflow](https://developers.google.com/google-ads/api/docs/oauth/single-user-authentication)
   (`gcloud auth application-default login` with your client file).
5. Put `client_id`, `client_secret` and `refresh_token` in `~/google-ads.yaml` (chmod 600), and
   `login_customer_id` if you reach the account through a manager account. Then run
   `adops-guard doctor google`.

Service accounts (the route Google's own quickstart now shows) are not supported yet.

**Meta** needs an access token with `ads_read` (reads) and `ads_management` (writes). A system user
token from Business settings, limited to the one ad account, is the safest choice. Pick one source:
these lines are already in the `[meta]` section of the example config, commented out; uncomment the
ones you use rather than adding a second `[meta]` block.

```ini
[meta]
# Pick one source and uncomment its lines. With none, $META_ACCESS_TOKEN is read.
# token_source = env           # META_ACCESS_TOKEN (or token_env = ANOTHER_NAME)

# token_source = file          # a file only you can read
# token_file = ~/.config/adops-guard/meta-token

# token_source = keychain      # macOS: security find-generic-password -s SERVICE [-a ACCOUNT] -w
# keychain_service = adops-guard-meta

# token_source = ssm           # AWS SSM Parameter Store, via the aws CLI and your AWS profile
# ssm_parameter = /adops-guard/meta-token
# ssm_region = us-east-1
```

To store a token without it landing in your shell history or on screen
([step by step](docs/setup.md#step-6-store-the-token)): in the macOS Keychain, type this line first,
then copy the token, then press Enter, and clear the clipboard afterwards with `pbcopy </dev/null`:

```bash
security add-generic-password -U -a "$USER" -s adops-guard-meta -w "$(pbpaste)"
```

Do not leave `-w` without a value: `security` then prompts for the token and silently cuts it at 128
characters (Meta tokens are about 200), and `adops-guard doctor` points this out when Meta refuses a
128-character token. Do not paste the line together with other lines either: the clipboard would
hold the command text, and that would be stored. Or use a file only you can read:

```bash
mkdir -p ~/.config/adops-guard
(umask 077 && read -rs TOKEN && printf '%s' "$TOKEN" > ~/.config/adops-guard/meta-token)   # paste, Enter
```

For SSM, create a `SecureString` parameter in the AWS console or with `aws ssm put-parameter
--cli-input-json file://...` from a private file you delete afterwards; adops-guard reads it with
`aws ssm get-parameter --with-decryption`.

## Examples

**Lower a budget when total spend reaches a target.** Without `--apply` the watch runs the same way
and only prints what it would do at the target.

```text
$ adops-guard google spend-watch --target 1500 --then-budget 11111111111=10 --apply
WATCH    123-456-7890: target 1,500.00 USD (source: max)
         then: set campaign 11111111111 daily budget to 10
CHECK    Campaign 11111111111 "Spring promo": daily budget 40.00 USD -> 10.00 USD: validated (validate only)
2030-05-03 11:45 UTC  spend 1,465.00 USD (98% of target)  billing 1,180.00  metrics 1,465.00 since 2030-04-01  account limit 5,000.00
2030-05-03 12:00 UTC  spend 1,510.00 USD (101% of target)  billing 1,180.00  metrics 1,510.00 since 2030-04-01  account limit 5,000.00
TARGET   reached: 1,510.00 USD >= 1,500.00 USD
PLAN     google budget.set  customers/1234567890/campaignBudgets/911111111111
         Campaign 11111111111 "Spring promo": daily budget 40.00 USD -> 10.00 USD
CHECK    validated by the API (validate only); nothing was written
APPLY    sent
VERIFY   read back 10.00 USD: OK

$ adops-guard google spend-watch --target 1500 --then-budget 11111111111=10 --apply
NO-OP    this spend watch already fired at 2030-05-03T12:00:00Z (spend 1510)
         pass --reset to arm it again
```

Here the billing counter (1,180.00) lags the real-time metrics (1,510.00); the watch acts on the
larger one, counted from the account budget's start (2030-04-01). On an account **without** an active
account budget (automatic payments) there is nothing to count from, so the watch refuses to start
until you say where to count from, e.g. `--since 2030-05-01` (a day in the account's time zone). With
`--since`, only the real-time metrics count, because the billing counter keeps its own start date.
At the target, `--then-pause` actions run first, then `--then-budget`; each runs on its own, so one
failure does not stop the others, and a watch with a failed action is not marked as fired.

Run it in `tmux`, as a [systemd user service](examples/systemd/adops-guard-spend-watch.service) (Linux),
as a [launchd agent](examples/launchd/com.example.adops-guard.spend-watch.plist) (macOS), or from
[cron](examples/crontab.example) with `--once`.

**Turn off YouTube in-stream for a Demand Gen ad group:**

```text
$ adops-guard google channel-controls --ad-group 22222222222 --youtube-in-stream off
Ad group 22222222222 "Group A" (DEMAND_GEN): youtube-in-stream on, youtube-in-feed on, youtube-shorts on, discover on, gmail off, display off
PLAN     google channel-controls.set  customers/1234567890/adGroups/22222222222
         Ad group 22222222222 "Group A": youtube-in-stream on -> off
         leaf field masks: only the channels named above are sent; the others stay as they are
CHECK    validated by the API (validate only); nothing was written
DRY RUN  nothing changed. Re-run with --apply to make this change.
```

**Activate a Meta ad, checked against the ad set's budget:**

```text
$ adops-guard meta set status 120000000000000003 ACTIVE
PLAN     meta ad.status  120000000000000003
         ad 120000000000000003 "Video 1": PAUSED -> ACTIVE
         within the ceiling: the daily budget (ad set 120000000000000002) 20.00 USD <= 50.00 USD ([meta] max_daily_budget)
         a daily budget is not a hard cap (Meta may spend more on some days); use a spend cap for a hard limit
CHECK    not sent to the API: add --validate to have the platform check it without writing
DRY RUN  nothing changed. Re-run with --apply to make this change.

$ adops-guard meta set status 120000000000000003 ACTIVE --validate
...
CHECK    validated by the API (validate only); nothing was written
DRY RUN  nothing changed. Re-run with --apply to make this change.
```

**Check videos before they go into Instagram placements:**

```text
$ adops-guard meta video-check 120000000000000009 120000000000000010 --file cut-b.mp4
  video 120000000000000009: eligible for Instagram placements | 14.80 s | ready | Cut A
  video 120000000000000010: NOT eligible for Instagram placements | 18.00 s (at or over the observed limit) | ready | Cut B
  file cut-b.mp4: LIKELY NOT eligible (18.00 s; limit 15 s)
  note: observed (Oct 2026): videos of about 17 to 19 s came back is_instagram_eligible=false, and a 14.8 s cut of the same video came back true. The exact limit was not tested: videos of --max-seconds (15) or longer are flagged. Meta does not document this; it may change.
```

**Create a website conversion action and wire up the landing page:**

```text
$ adops-guard google conversion-action create --name "Website: App Store button" --apply
PLAN     google conversion-action.create  customers/1234567890/conversionActions
         create WEBPAGE conversion action "Website: App Store button" (category OUTBOUND_CLICK, ONE_PER_CLICK, value 1, click window 30 d, view window 1 d)
         secondary action: reported in Google Ads, not used for bidding (pass --primary to change)
CHECK    validated by the API (validate only); nothing was written
APPLY    sent
VERIFY   read back "Website: App Store button" WEBPAGE OUTBOUND_CLICK ENABLED secondary: OK
CREATED  customers/1234567890/conversionActions/1
         send_to for snippets/landing-consent-conversion.html: AW-000000000/FAKE_LABEL_1
```

Put that `send_to` value, your Google tag ID, App Store ID and provider token into
[`snippets/landing-consent-conversion.html`](snippets/landing-consent-conversion.html), and use
`https://your.site/?ct=spring-video` as the ad's final URL: App Store Connect then counts downloads
for the `spring-video` campaign.

**Daily audit from cron, silent unless something fires:**

```cron
15 8 * * * cd "$HOME/adops" && adops-guard --config adops-guard.ini google audit --days 7 --fail-on-signal > audit.txt 2>&1 || cat audit.txt
```

**Machine-readable output.** With `--json`, stdout carries exactly one JSON document, also when the
command fails: then it is `{"error": "...", "hint": "...", "exit_code": 3}`. Human-readable lines go
to stderr.

**Journal.** Each applied write leaves three lines in `.adops-guard/journal.jsonl`:

```json
{"ts": "2030-05-03T12:00:01.112233Z", "run": "457187e0c715", "platform": "google", "action": "budget.set", "target": "customers/1234567890/campaignBudgets/911111111111", "phase": "intent", "description": "Campaign 11111111111 \"Spring promo\": daily budget 40.00 USD -> 10.00 USD", "request": {"service": "campaignBudgets", "operations": [{"update": {"resourceName": "customers/1234567890/campaignBudgets/911111111111", "amountMicros": "10000000"}, "updateMask": "amount_micros"}]}}
{"ts": "2030-05-03T12:00:01.412233Z", "run": "457187e0c715", "platform": "google", "action": "budget.set", "target": "customers/1234567890/campaignBudgets/911111111111", "phase": "applied", "response": {"results": [{"resourceName": "customers/1234567890/campaignBudgets/911111111111"}]}}
{"ts": "2030-05-03T12:00:01.712233Z", "run": "457187e0c715", "platform": "google", "action": "budget.set", "target": "customers/1234567890/campaignBudgets/911111111111", "phase": "verified", "expected": 10000000, "got": 10000000}
```

The outputs above come from the test suite's fake APIs; IDs and names are placeholders.

## Security model

What adops-guard guarantees, and how:

- **Nothing is written by default.** Every write command is a dry run unless `--apply` is given. A
  Google dry run asks the platform to validate the exact request (`validateOnly`); a Meta dry run
  sends nothing unless you add `--validate` (then `execution_options=["validate_only"]`), because
  that mode is not yet verified live for updates (see [How a write works](#how-a-write-works)).
  Whenever a dry run sends a validate-only request, the object is read before and after it; if it
  changed, the command stops with exit 4 and journals `dry-run-wrote`. With `--apply`, the
  validate-only request always goes first, so the real write is never your first contact with the
  API's rules.
- **Spend increases need a ceiling.** Raising a budget or a spend cap above the configured ceiling,
  removing a spend cap, or enabling a campaign, ad set or ad whose budget is above the ceiling is
  refused before anything is written. Without a configured ceiling every increase is refused (fail
  closed). `--override-limit` skips the ceiling for one command and says so in the plan. Lowering
  spend is never blocked.
- **Writes stay in one account.** Google writes go to the configured customer only; Meta writes are
  refused for objects whose `account_id` is not the configured ad account.
- **Every write is verified.** After `--apply` the object is read back and compared with what was
  sent; a mismatch exits with code 4 and is journaled.
- **Every write is journaled.** `journal.jsonl` gets `intent` before the request and `applied`,
  `error` or `ambiguous` after it, then `verified` or `mismatch`. Requests and responses are redacted
  before they are written.
- **Idempotent and single-writer.** The current value is read before every write; an unchanged value
  is a no-op. A non-blocking `flock` lets only one write run at a time. `spend-watch` records that it
  fired and never acts twice.
- **No blind retries of writes.** Google: reads and validate-only calls are retried on 429/5xx and
  network errors. Meta: reads and validate-only calls are retried on transient errors (HTTP 5xx,
  `is_transient`, codes 1 and 2) and network errors; a rate limit stops the command with exit 5.
  Real writes are never retried: a 5xx, a timeout or a broken connection during a write is reported
  as "may or may not have been applied" and journaled as `ambiguous`. A failed OAuth token refresh
  happens before anything is sent, so it is retried and never counts as an ambiguous write.
- **Credentials only where they belong.** Tokens are read from environment variables, files with mode
  600 owned by you, the macOS Keychain or AWS SSM. No command-line option accepts a secret (a test
  enforces this). The config file refuses keys that look like secrets, and a config file that other
  users can write to, or that belongs to another user, is refused.
- **Credentials never leak into output.** Secrets live in a wrapper that prints as `***` and refuses
  JSON encoding and pickling. Every line of output, every error, the journal and the state file pass
  through a redactor that masks every loaded secret and well-known token shapes (`EAA...`,
  `ya29....`, `1//...`, `GOCSPX-...`, `Bearer ...`, `access_token=...`), in values, in keys and in
  anything converted to text on the way. Unexpected errors print a one-line message; `--debug` shows
  a redacted traceback.
- **Tokens only go to the official hosts.** Requests carrying credentials go over HTTPS to
  `googleads.googleapis.com`, `oauth2.googleapis.com` or `graph.facebook.com`. The offline tests need
  a loopback address; that is allowed only with `ADOPS_GUARD_TEST_ENDPOINTS=1` in the environment
  (never by a config file alone, so a config planted in a directory you run the tool from cannot
  redirect your tokens) and is announced with a warning. Redirects are never followed. Tokens travel
  in headers (`Authorization`, and `developer-token` if you set one) or, for the OAuth refresh, in a
  POST body: never in a URL. Meta paging uses cursors, never the `next` URLs.
- **Private files.** The state directory is created 0700; the journal, state file and lock are 0600.
  An existing state directory must be a real directory (not a symlink), owned by you and not writable
  by others. The journal and the lock are opened without following symlinks, and refused if they have
  extra hard links, so nobody can redirect a write into another of your files.
- **Rate-limit guard.** Meta requests stop when the usage headers reach `usage_stop_percent`.
- **No telemetry.** The only network traffic is to the APIs you call.

What it does **not** protect against: a compromised machine or account; a token with more access
than it needs; mistakes in the ceilings you configure (and ceilings are per budget, not an account
total); platform-side overspend (daily budgets are not caps on either platform); spend between two
`spend-watch` polls; and running it inside a checkout whose files you did not write yourself (a
`./adops-guard.ini` there still sets your ceilings and where the Meta token is read from, though
tokens only ever go to the official hosts; pass `--config` to be explicit). Use platform-side limits
as well: an account budget on Google Ads, the account spending limit and campaign spend caps on Meta.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, dry run, or nothing to do |
| 1 | API, network or unexpected error (including "the write may or may not have been applied"), or a `doctor` check failed |
| 2 | Usage, configuration or credentials setup error |
| 3 | Refused by a guard (ceiling, account scope, shared budget, lock busy), or a check failed (`audit --fail-on-signal`, `video-check`) |
| 4 | The value read back after a write is not what was sent, or an object changed during a dry run's validate-only request |
| 5 | Rate limited by the platform or stopped by the usage guard: retry later |

## Limitations and honest caveats

- **Version 0.1.0 is alpha.** This code is a rewrite and generalisation of scripts we used against the
  live Google Ads API (v25) and Graph API (v26.0) in September and October 2026. The open-source
  version is tested offline against fake APIs; this exact code has not been run against the live APIs
  yet. Start with read-only commands and dry runs, and report what you find.
- **Doctor checks degrade to manual ones.** Like the rest of 0.1.0, `doctor` has run only against
  the offline fakes. When one of its optional reads fails (the Meta app, the Instagram identity, the
  advertisable apps, Google's auto-apply settings), the check becomes a manual one, with the click
  path, instead of failing. The Meta app mode (Development or Live) is always a manual check: the
  API does not report it.
- **Meta validate-only on updates is unverified.** Meta documents `execution_options=["validate_only"]`
  for updates, but we have used it live only for creating objects. That is why a Meta dry run sends
  nothing unless you pass `--validate`, and why every dry-run validation is checked by reading the
  object before and after.
- **Small scope.** No campaign, ad set, ad or creative creation, no uploads, no placement exclusions,
  no Apple Search Ads, TikTok or other platforms.
- **Platform behaviour changes.** The gotchas docs say when each thing was observed. Field
  availability (the audit's segments in particular) depends on the API version and campaign type; the
  audit reports what it could not read.
- **spend-watch is a poller.** Spend can overshoot between polls, and both counters have reporting
  delays. It is a safety net on top of platform limits, not a replacement for them. Without an active
  account budget it needs `--since`, and `--max-hours` counts from each start of the process.
- **The audit is a heuristic.** Name matching is crude and English/Spanish/Portuguese only by default;
  thresholds are starting points. A fired signal is a reason to look, not a verdict.
- **Google:** OAuth refresh tokens only (no service accounts); flat `google-ads.yaml` parsing only;
  budgets set as daily amounts (no total-amount budgets). Google's access model moved from developer
  tokens to Google Cloud projects in September 2026; the code follows Google's notes on that change,
  not live tests.
- **Meta:** the account-level spending limit is shown but not changed (set it in Ads Manager);
  `whoami` does not show token expiry (Meta's `debug_token` would need the token in a URL); currency
  offsets are built in for common currencies only (set `currency_offset` otherwise); the usage guard
  depends on the headers Meta sends.
- **Platforms:** developed and tested on Linux and macOS. On Windows, file permission checks are
  skipped and locking is untested.

## FAQ

**Why not use the official client libraries?**
They are excellent and much broader, but they pull in gRPC/protobuf (Google) or large SDKs, and they
act immediately. adops-guard needs a dozen REST calls, so it uses `urllib` and stays small enough to
read in an afternoon.

**Does it work through a manager (MCC) account?**
Yes: set `login_customer_id` (config, `google-ads.yaml` or `--login-customer-id`) and `customer_id` to
the client account. `google accounts` lists what you can reach.

**What access do the credentials need?**
Google: a Google Cloud project with Google Ads API access at Explorer level or higher (Test access
reaches test accounts only) and an OAuth user with access to the account; see
[Getting Google Ads credentials](#getting-google-ads-credentials). Meta: `ads_read` for reading and
`ads_management` for writes; prefer a system user limited to one ad account. `adops-guard doctor`
checks both platforms and names what is missing.

**What happens if `--apply` fails half-way?**
The journal shows how far it got. If the platform's answer was ambiguous (timeout, 5xx), run the same
command again: it reads the current value and writes only if needed. The state file keeps a `pending`
marker for that target until a later run settles it: if the value is already in place, that run
journals `verified` for the earlier write ("the earlier --apply ... is confirmed in place"); if it
still has to write, it warns first ("an earlier --apply for this target did not finish").

**Can I run it unattended?**
Yes: `--json` for machine output, exit codes for alerts, `spend-watch --once` from cron, the systemd
unit in [`examples/systemd`](examples/systemd) or the launchd agent in
[`examples/launchd`](examples/launchd). Unattended writes still go through the same ceilings and
checks. Install a pinned release tag for this, not whatever `main` is today.

**Where is the journal, and what is in it?**
In the state directory (`.adops-guard/` next to your config file by default): `journal.jsonl`
(append-only), `state.json` (idempotency records) and `lock`. They contain object IDs, names and
amounts, but never credentials. Keep them out of version control (the `.gitignore` in this repo does)
and treat them as private: they describe your campaigns.

**How do I make overspending truly impossible?**
You can't from a client alone. Combine platform-side limits (Google account budget, Meta account
spending limit and campaign spend caps) with adops-guard's ceilings and `spend-watch`.

**Why is the output plain ASCII?**
It ends up in cron mail, `journalctl` and CI logs, where fancy characters break. The one exception is
`doctor` in a terminal, which marks its checks ✓ and ✗; piped or logged, it writes `ok` and `FAIL`.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest            # or: python -m unittest discover -s tests
ruff check . && ruff format --check .
```

Tests are fully offline: a local HTTP server on 127.0.0.1 plays the Google Ads REST API and the Graph
API, and the snippet's JavaScript runs in Node.js when it is installed. See
[CONTRIBUTING.md](CONTRIBUTING.md). Please report security problems privately, see
[SECURITY.md](SECURITY.md).

More docs: [setup from zero](docs/setup.md) · [troubleshooting](docs/troubleshooting.md) ·
[click-quality audit](docs/click-quality-audit.md) · [Google Ads gotchas](docs/google-gotchas.md) ·
[Meta gotchas](docs/meta-gotchas.md) · [changelog](CHANGELOG.md)

## About / Built by DEEGITECH

adops-guard is built and maintained by [DEEGITECH](https://github.com/deegitech)
(DEEGITECH Teknoloji ve Yazılım Ltd. Şti.), a small software and game studio. We wrote the first
version of these tools while launching our iOS game **Wide Molly Hooked**, a one-touch climbing game
for iPhone and iPad ([App Store](https://apps.apple.com/app/id6813081261) ·
[website](https://widemolly.com)), and open-sourced the parts that kept our own budget safe.

Issues and pull requests are welcome.

## License

[MIT](LICENSE) © 2026 DEEGITECH Teknoloji ve Yazılım Ltd. Şti.

Not affiliated with or endorsed by Google or Meta. Google Ads, YouTube, Meta, Facebook and Instagram
are trademarks of their owners.
