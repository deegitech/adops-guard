# Setup from zero (about 15 minutes)

This guide takes you from nothing to a green `adops-guard doctor` and a first dry run, for Google
Ads, for Meta, or for both. The console steps come from running these tools in production in
September and October 2026, where a person clicked through each of them, and from Google's and Meta's
documentation where a flow has changed since (Google moved API access to the Cloud project on
9 September 2026).

- **Console menus get renamed often.** Each step gives the click path and the usual label variants;
  names may differ in your console. Search the page for the label if a menu has moved.
- Platform behaviour that is not in the official documentation is marked **observed** with the
  month we saw it.
- Every ID below is a placeholder: `123-456-7890`, `1234567890`, `act_123456789012345`,
  `com.example.mygame`, `17841401234567890`, `you@example.com`.
- Something failed? Run `adops-guard doctor`: every failed check prints its fix. Every error message
  and its fix are also in [troubleshooting.md](troubleshooting.md).

**Time:** about 5 minutes for step 0, then about 15 minutes per platform once the accounts exist. The
first Cloud project or Meta app can take longer, and Google may not grant API access at once: apply for
it first ([Google step 2](#step-2-get-an-api-access-level-that-reaches-your-account)), then do the
other steps while you wait.

## Contents

- [What you need](#what-you-need)
- [0. Install adops-guard and create the config file](#0-install-adops-guard-and-create-the-config-file)
- [Google Ads](#google-ads), steps 1 to 9
- [Meta](#meta), steps 1 to 10
- [Token expiry and renewal](#token-expiry-and-renewal)
- [Running on a server](#running-on-a-server)
- [Common mistakes](#common-mistakes)

## What you need

| | Google Ads | Meta |
|---|---|---|
| Ad account | A Google Ads account and its 10-digit customer ID (`123-456-7890`, shown next to the account name at the top of the Google Ads UI). Optional: a manager (MCC) account above it. | An ad account (`act_123456789012345`) in a Meta Business portfolio (Business Manager). |
| Your access | A Google account with **Standard** or **Admin** access to the Ads account. **Read only** is enough for reports, not for changes. | A Facebook account with **Manage campaigns** (partial access) or **full control** of the ad account, and admin of the Meta app you create. |
| Developer side | A Google Cloud project (free). | A Meta developer account (free: register at developers.facebook.com) and a Meta app. |
| Access tier | Google Ads API access level **Explorer** or higher to reach real accounts. New Cloud projects start on **Test**, which reaches test accounts only. | The Marketing API tier **development_access** (the default for new apps) is enough to start. |
| Tools | Python 3.10 or newer; the [gcloud CLI](https://cloud.google.com/sdk/docs/install), only to create the refresh token. | Python 3.10 or newer; FFmpeg (`ffprobe`) only for `meta video-check --file`. |
| Only if you create ads | | A Facebook Page, an Instagram professional account linked to it, and for Live mode a privacy policy URL, a category and a 1024 x 1024 app icon. |

adops-guard itself only reads and changes budgets, statuses, spend caps, channel controls and
conversion actions; it does not create campaigns or ads. The Page, Instagram, app-promotion and
Live-mode steps below are there because they are the walls you hit next, and `adops-guard doctor`
checks them for you.

## 0. Install adops-guard and create the config file

```bash
pipx install "git+https://github.com/deegitech/adops-guard.git@v0.1.0"
mkdir -p ~/adops && cd ~/adops
curl -fsSLO https://raw.githubusercontent.com/deegitech/adops-guard/v0.1.0/examples/adops-guard.example.ini
mv adops-guard.example.ini adops-guard.ini
chmod go-w adops-guard.ini        # a config file other users can change is refused
adops-guard doctor                # what is not set up yet shows as skipped
```

The example leaves the account IDs and the token source commented out (`#`), so the doctor skips a
platform until you set it up, and a placeholder ID is never sent to an API. Using only one platform?
Delete the other platform's section from the file.

Open `adops-guard.ini` and change the **ceilings** to your own numbers: the largest budget or spend
cap any command may set. Without a ceiling every spend increase is refused; that is on purpose. The
account IDs come later (Google step 6, Meta step 7). The file never holds secrets (keys that look like
secrets are refused).

**Edit the lines that are in the file; do not paste a second `[google]` or `[meta]` block.** Each
section and each key may appear only once, so a pasted block stops every command with `section 'meta'
already exists`. The steps below say which lines to change or uncomment.

---

## Google Ads

### Step 1. Create a Cloud project and enable the Google Ads API

1. Open [console.cloud.google.com](https://console.cloud.google.com/) → project picker in the top
   bar → **New project** → a name such as `adops` → **Create**. Make sure the new project is
   selected.
2. **APIs & Services** → **Library** (or **Enable APIs and services**) → search **Google Ads API**
   → **Enable**.

### Step 2. Get an API access level that reaches your account

Since **9 September 2026** Google no longer uses developer tokens: the access level belongs to the
Cloud project that issues your OAuth credentials
([Google's note](https://developers.google.com/google-ads/api/docs/get-started/dev-token)).

1. Open the **Google Ads API Overview** page of the project:
   [console.cloud.google.com/google/ads-apis/overview](https://console.cloud.google.com/google/ads-apis/overview).
2. A new project shows **Test** access (test accounts only). Expand **Upgrade access level**, check
   that the next level says **Explorer**, and click **Apply for access**.
3. Google may upgrade the project to Explorer automatically right after you apply. If it does not,
   wait for Google's answer: the access-levels page gives no time for Explorer (the manual review for
   Standard access takes about 10 business days). Do steps 3 to 7 while you wait.

Until Explorer is granted, every call to a real account fails with
`CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` (API v25; older API versions answer
`ACTION_NOT_PERMITTED`), and `adops-guard doctor` points you back to this step.

| Level | Reaches | Operations a day | Notes |
|---|---|---|---|
| Test | test accounts | 15,000 | the default for a new project |
| Explorer | test and production accounts | 2,880 on production accounts | no Keyword Planner, audience insights, reach planning or billing/payments services; enough for adops-guard |
| Basic | test and production accounts | 15,000 | needs brand verification of the Cloud project first |
| Standard | test and production accounts | unlimited | a manual review |

When the daily quota is used up, the API answers HTTP 429 `RESOURCE_EXHAUSTED` (**observed, Oct
2026**) until the 24-hour window rolls over; adops-guard stops with exit code 5 and you re-run
later. Details: [access levels](https://developers.google.com/google-ads/api/docs/api-policy/access-levels).

> **Older guides** tell you to get a developer token in Google Ads → **Tools** → **API Center** (a
> manager account worked best). That was the flow before September 2026. A token from there still
> works but is ignored; you can leave it out of `google-ads.yaml`.

### Step 3. Configure the OAuth consent screen and create a Desktop client

Newer consoles call this area **Google Auth Platform**; older ones have it under **APIs & Services
→ OAuth consent screen** and **APIs & Services → Credentials**. Names may differ.

1. **Google Auth Platform** → **Get started** (or **OAuth consent screen**): app name, a support
   e-mail such as `you@example.com`, **Audience: External** (Internal only works inside a Google
   Workspace organisation), a contact e-mail → **Create**.
2. **Data access** (or **Scopes**) → **Add or remove scopes** → add
   `https://www.googleapis.com/auth/adwords` → **Update** → **Save**.
3. **Audience** → **Test users** → add the Google account that has access to your Ads account.
4. **Clients** (or **Credentials → Create credentials → OAuth client ID**) → **Create client** →
   Application type **Desktop app** → a name → **Create** → **Download JSON**. Save it as
   `credentials.json` outside any repository, then `chmod 600 credentials.json`. Keep it: you need
   it again to renew the refresh token, and newer consoles may not show the client secret again.
5. **Audience** → Publishing status → **Publish app** (moves it from *Testing* to *In production*).
   While the app stays in *Testing*, Google expires its refresh tokens after **7 days**
   (`invalid_grant`). Google may then warn that the app is not verified when you sign in; for your
   own app you can continue (**Advanced** → **Go to** your app; names may differ).

### Step 4. Create the refresh token

Google's single-user guide uses the gcloud CLI with the Desktop client from step 3:

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform \
  --client-id-file=credentials.json
```

A browser opens: sign in with the Google account that has access to the Ads account and allow
access. gcloud saves `client_id`, `client_secret` and `refresh_token` in
`~/.config/gcloud/application_default_credentials.json` (Windows:
`%APPDATA%\gcloud\application_default_credentials.json`); it prints the file name, not the values.
Details:
[single-user authentication](https://developers.google.com/google-ads/api/docs/oauth/single-user-authentication).

### Step 5. Store the credentials (a 0600 file)

Copy the three values into `~/google-ads.yaml` without showing them on screen. Google's own client
libraries read the same file, so if it already exists, the script below only replaces the
`client_id`, `client_secret` and `refresh_token` lines and keeps everything else in it
(`login_customer_id`, `developer_token`, `use_proto_plus` and so on):

```bash
(
  umask 077
  python3 - <<'EOF'
import json, pathlib, re
home = pathlib.Path.home()
adc = json.loads((home / ".config/gcloud/application_default_credentials.json").read_text())
keys = ("client_id", "client_secret", "refresh_token")
path = home / "google-ads.yaml"
old = path.read_text().splitlines() if path.exists() else []
if path.exists():
    path.chmod(0o600)  # private before the secrets go in
kept = [line for line in old if not re.match(r"(client_id|client_secret|refresh_token)\s*:", line)]
path.write_text("".join(f"{key}: {adc[key]}\n" for key in keys) + "".join(f"{line}\n" for line in kept))
EOF
)
chmod 600 ~/google-ads.yaml
```

- If you reach the account through a manager (MCC) account, add its ID: `login_customer_id:
  1234567890` in `google-ads.yaml`, or `login_customer_id = 123-456-7890` under `[google]` in the
  config file.
- If nothing else on this machine uses Application Default Credentials, delete gcloud's copy:
  `rm ~/.config/gcloud/application_default_credentials.json`. Do **not** run
  `gcloud auth application-default revoke`: that revokes the refresh token you just copied.
- adops-guard refuses a credentials file that other users can read or that someone else owns.

Other places for the same three values:

- **Environment variables:** `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET` and
  `GOOGLE_ADS_REFRESH_TOKEN` (all three, or none; they win over any file). Good for CI secrets.
- **macOS Keychain:** adops-guard reads Google credentials only from the environment or the file,
  so keep the file, or export the variables from the Keychain in a wrapper script. Export all three:
  the variables are all or nothing (with only some of them set, adops-guard stops with `some
  GOOGLE_ADS_* variables are set but these are missing`).

  ```bash
  #!/bin/sh
  export GOOGLE_ADS_CLIENT_ID="$(security find-generic-password -s adops-guard-google-client-id -w)"
  export GOOGLE_ADS_CLIENT_SECRET="$(security find-generic-password -s adops-guard-google-client-secret -w)"
  export GOOGLE_ADS_REFRESH_TOKEN="$(security find-generic-password -s adops-guard-google-refresh-token -w)"
  exec adops-guard "$@"
  ```

  To store each value, use the same `-w "$(pbpaste)"` form as in
  [Meta step 6](#step-6-store-the-token).
- **AWS SSM on a server:** see [Running on a server](#running-on-a-server).

### Step 6. Fill in the config file

In the `[google]` section of `adops-guard.ini`, remove the `#` in front of `customer_id` and put in
your account; set `max_daily_budget` to your own ceiling. Uncomment `login_customer_id` only if you
reach the account through a manager (MCC) account. Without its comments, the section then reads:

```ini
[google]
customer_id = 123-456-7890
# login_customer_id = 123-456-7890
api_version = v25
max_daily_budget = 50.00
```

`adops-guard google accounts` lists the accounts the OAuth user reaches, including the ones under a
manager account.

### Step 7. Check the account budget, auto-apply and conversion goals (Google Ads UI)

1. **Account budget (monthly invoicing).** Google Ads → **Billing** (tools menu; names may differ)
   → **Account budget**. Campaigns **stop** when total spend reaches the approved spending limit,
   even if promotional credit is waiting. Raise it with **Edit** before you get close. On automatic
   payments there is no account budget, and `spend-watch` then needs `--since`.
   - The billing counter (`account_budget.amount_served_micros`) can lag real-time spend by hours
     (**observed, Oct 2026**). adops-guard uses the larger of it and the sum of
     `metrics.cost_micros`.
   - Promotional "spend X, get X" offers count the **served** amount. Keep a margin above the
     threshold: refunds for invalid clicks can pull you back under it.
2. **Auto-apply.** Google Ads → **Campaigns** → **Recommendations** → **Auto-apply** (top right;
   names may differ) → turn off the types you manage yourself. Otherwise Google may change settings
   silently, such as turning a YouTube channel back on (**observed, Oct 2026**).
3. **Conversions for an iOS app without an SDK** (optional). Use a landing page whose App Store
   button fires a `WEBPAGE` conversion action of category `OUTBOUND_CLICK`, with Consent Mode v2:
   [`snippets/landing-consent-conversion.html`](../snippets/landing-consent-conversion.html) and
   `adops-guard google conversion-action create --name "Website: App Store button"` (dry run first,
   then `--apply`).
   - An account-default goal whose only action is **secondary** shows as **Misconfigured**
     (**observed, Oct 2026**). If
     campaigns should bid on the action, make it primary (`--primary`, or Goals → Conversions →
     Summary → the action → **Edit settings** → **Primary action**) and set **campaign-level**
     conversion goals so unrelated campaigns are not affected.
4. **Never click your own ads, or ask friends to.** Those are invalid clicks: they do not count, and
   they put credits and the account at risk.

### Step 8. Verify with `adops-guard doctor google`

```text
$ adops-guard doctor google
adops-guard 0.1.0 doctor: read-only checks; nothing is changed on either platform

Setup
  ✓ config file /home/you/adops/adops-guard.ini (found in the working directory): valid, not writable by other users
  · state directory /home/you/adops/.adops-guard: not there yet (the first write creates it, mode 0700)

Google Ads
  ✓ ceiling: [google] max_daily_budget = 50.00 per budget
  ✓ credentials from /home/you/google-ads.yaml (mode 0600, owned by you): client_id, client_secret and refresh_token are set
  ✓ OAuth: Google accepted the refresh token
  ✓ the OAuth user reaches 1 account(s) directly: 123-456-7890
  ✓ account 123-456-7890 "Example Co": ENABLED, USD, America/New_York
  ✓ API access level: Explorer or higher (a production account answered)
    tip: the level is shown on Cloud console > Google Ads API > Overview; Explorer allows 2,880 operations a day on production accounts, Basic 15,000 (it needs brand verification)
  ✓ account budget: 1,465.00 USD of 5,000.00 USD used since 2030-04-01 (the larger of billing 1,180.00 and metrics 1,465.00; billing can lag for hours), 3,535.00 USD left
  ! auto-apply is on for 1 recommendation type(s): KEYWORD
    fix: Google Ads > Campaigns > Recommendations > Auto-apply: turn off the types you manage yourself (Google may otherwise change settings by itself, e.g. turn channels back on)
  ✓ conversion goals: 1 of 1 enabled conversion action(s) are primary

Result: 9 passed, 0 failed, 1 warning(s)
```

Each ✗ comes with a `fix:` line; fix it and run the doctor again. Warnings (`!`) and manual checks
(`?`) do not fail it. The doctor only reads: it exchanges the refresh token for an access token and
runs a few queries.

### Step 9. Your first real command (a dry run)

```bash
adops-guard google status                                          # account, account budgets, campaigns
adops-guard google report --by campaign --days 7
adops-guard google budget set --campaign 11111111111 --amount 25   # dry run: plan + validate-only, nothing written
```

Add `--apply` only when the plan is what you want.

---

## Meta

### Step 1. Create a Meta app with the Marketing API use case

1. [developers.facebook.com](https://developers.facebook.com/) → **My Apps** → **Create app**
   (or reuse an app of type **Business**). Connect it to the Business portfolio that owns the ad
   account when asked.
2. Add the use case **Create & manage ads with Marketing API** (App dashboard → **Use cases** →
   **Add use case**, or pick it while creating the app; names may differ).
   - **Observed (Sep 2026):** the use case *Create & manage app ads* does not expose `ads_management`
     or `ads_read` in Graph API Explorer. Pick the Marketing API one.

### Step 2. Add the permissions

App dashboard → **Use cases** → **Create & manage ads with Marketing API** → **Customize** →
permissions: add `ads_management` and `ads_read`. For the doctor's Page and Instagram checks, and
for linking assets, also add `business_management`, `pages_show_list` and `pages_read_engagement`.
**Observed:** permissions often have to be added one at a time.

### Step 3. Switch the app to Live

1. App dashboard → **App settings** → **Basic**: fill in **Privacy Policy URL**, **Category** and
   **App icon** (1024 x 1024) → **Save changes**.
2. Switch **App Mode** to **Live** (the toggle in the top bar, or **Publish** on the dashboard;
   names may differ).

**Observed (Oct 2026):** while the app is in Development mode, every ad creative made through it
fails with error `100/1885183` ("the creative post was created by an app in development mode"). The
API does not report the app mode, so the doctor lists this as a manual check.

### Step 4. Generate a user token in Graph API Explorer

First give the token's user access to the ad account, as in
[step 8.1](#step-8-check-the-ad-account-ads-manager-and-business-settings): Business settings →
**Accounts** → **Ad accounts** → your ad account → **Assign people** → your user → **Manage
campaigns**. The token dialog below only offers assets the user can already reach.

1. developers.facebook.com, top menu → **Tools** → **Graph API Explorer**
   ([developers.facebook.com/tools/explorer](https://developers.facebook.com/tools/explorer/)).
2. **Meta App:** your app. **User or Page:** **User Token** (or **Get User Access Token**).
3. **Permissions:** add `ads_management`, `ads_read`, `business_management`, `pages_show_list` and
   `pages_read_engagement`. The picker is sometimes a free-text box rather than a list: type each
   name.
4. **Generate Access Token**. In the Facebook dialog, tick every asset you will use: the business,
   the ad account if it is listed, and **both the Facebook Page and the Instagram account**. Ticking
   only one of the two is a common mistake.

**Alternative for servers: a system user token.** Business settings → **Users** → **System users**
→ **Add** → **Assign assets**: your ad account (**Manage campaigns**) and the app → **Generate new
token** → pick the app, an expiry if asked, and `ads_management` and `ads_read`. It is limited to
the assets you assigned, which makes it the safest choice. Names may differ; see Meta's
documentation on system users.

- If Meta answers `(#270) This Ads API request is not allowed for apps with development access
  level`, the app is on the lowest Marketing API access tier: there the token's user must be an
  admin of both the app and the ad account. Give the system user full control of both assets (the
  ad account and the app), then generate a new token; or upgrade the app's tier (step 8.6).

### Step 5. Extend the token to about 60 days

Click the **(i)** next to the token → **Open in Access Token Tool** (the **Access Token Debugger**)
→ **Extend Access Token** at the bottom → copy the long-lived token. It starts with `EAA` and is
about 200 characters long. The debugger also shows when it **expires**: put a reminder in your
calendar about 10 days before that date.

### Step 6. Store the token

Never paste the token into a command you can see in your history, a config file or a chat. Pick one
place:

**macOS Keychain** (`token_source = keychain`)

1. Type this line, but do not press Enter yet:

   ```bash
   security add-generic-password -U -a "$USER" -s adops-guard-meta -w "$(pbpaste)"
   ```

2. Copy the token in the browser.
3. Press Enter. Then clear the clipboard: `pbcopy </dev/null`.

- **Gotcha:** if you leave `-w` without a value, `security` prompts for it and **silently cuts the
  value at 128 characters**. Meta tokens are about 200 characters, so always use the
  `-w "$(pbpaste)"` form. `adops-guard doctor` tells you when a refused token is exactly 128
  characters long.
- Paste the command line **first**, then copy the token, then press Enter. Never paste two lines at
  once: the first line would run with the clipboard, which then holds the command text, and store
  that as the token.
- `-U` replaces an existing item, which is what you want when you renew.
- The value is on the `security` command line for a moment (other users of the same Mac could see
  it with `ps`). On a shared machine, use a file instead.

Then, in the `[meta]` section of `adops-guard.ini`, remove the `#` in front of these two lines:

```ini
token_source = keychain
keychain_service = adops-guard-meta
# keychain_account = you        # the -a value; optional
```

**A file only you can read** (`token_source = file`)

```bash
mkdir -p ~/.config/adops-guard
# macOS: copy the token first, then run
(umask 077 && pbpaste > ~/.config/adops-guard/meta-token) && pbcopy </dev/null
# Linux: run, paste the token (it is not shown), press Enter
(umask 077 && read -rs TOKEN && printf '%s' "$TOKEN" > ~/.config/adops-guard/meta-token)
```

Then uncomment these two lines in the `[meta]` section:

```ini
token_source = file
token_file = ~/.config/adops-guard/meta-token
```

**An environment variable** (`token_source = env`, the default when no `token_source` line is
uncommented): for this shell only.

```bash
read -rs META_ACCESS_TOKEN && export META_ACCESS_TOKEN   # paste, Enter: not echoed, not in history
```

**AWS SSM Parameter Store** (`token_source = ssm`): see [Running on a server](#running-on-a-server).

### Step 7. Fill in the config file

In the `[meta]` section, remove the `#` in front of `ad_account_id` and put in yours (Ads Manager
shows it in the account menu); set the three ceilings to your own numbers, in the account currency.
Those lines then read:

```ini
ad_account_id = act_123456789012345
max_daily_budget = 50.00
max_lifetime_budget = 500.00
max_spend_cap = 1000.00
```

Meta stores money in the currency's minor units (the "offset": 100 for USD, EUR or GBP, 1 for JPY).
adops-guard converts for you and refuses a currency it has no offset for, unless you set
`currency_offset`. Budgets below the account's `min_daily_budget` are refused before anything is
sent.

### Step 8. Check the ad account (Ads Manager and Business settings)

1. **Access for the token's user:** Business settings → **Accounts** → **Ad accounts** → your ad
   account → **Assign people** (or **Add people**) → your user or system user → **Manage campaigns**.
   Do this before step 4. If you grant access after generating the token, generate and store it
   again (steps 4 to 6).
2. **Account spending limit:** Ads Manager → **Billing & payments** → **Payment settings** →
   **Account spending limit** → set it (names may differ). It is the only hard ceiling for the
   whole account; adops-guard shows it and warns when it is close, but never sets it.
3. **Prepaid or card:** the doctor shows `is_prepay_account`. A card-paid account is charged even if
   you "added funds" to another account, and prepaid funds cannot be moved between ad accounts.
4. **Daily budgets are not caps:** Meta can spend up to about 75% more than the daily budget on a
   single day. For a hard ceiling per campaign use a spend cap: `adops-guard meta spend-cap`.
5. **"Please verify your identity":** if Ads Manager shows this banner (API error `31/3858385`,
   which can arrive with HTTP 200), follow it. Running ads continue, but nothing can be created
   until it is done. **Observed (Oct 2026):** it sometimes cleared just by visiting Ads Manager;
   otherwise verify through [facebook.com/accountquality](https://www.facebook.com/accountquality).
6. **Rate tier:** new apps are on `development_access`, with low limits. adops-guard reads the
   `X-Business-Use-Case-Usage` and `X-Ad-Account-Usage` headers and stops at 85% usage. For higher
   limits, upgrade the **Marketing API Access Tier** feature (older consoles: **Ads Management
   Standard Access**): App dashboard → **App Review** → **Permissions and features**; names may
   differ. Meta's
   [authorization page](https://developers.facebook.com/docs/marketing-api/overview/authorization/)
   lists what the upgrade needs.
7. **If you manage campaigns by API, do not click the "create an ad from your top posts" boxes in
   Ads Manager.** **Observed (Oct 2026):** they create separate Traffic campaigns with large default
   budgets. Also **observed (Oct 2026):** follower or profile-visit campaigns did not produce
   installs.

### Step 9. App promotion only (App Install campaigns)

Skip this step unless you run `OUTCOME_APP_PROMOTION` campaigns.

1. **Add the iOS platform:** App dashboard → **App settings** → **Basic** → **Add platform** →
   **iOS** → bundle ID (`com.example.mygame`) and App Store ID (`1234567890`) → **Save changes**.
2. **Let the ad account advertise the app:** Business settings → **Accounts** → **Apps** → your app
   → **Add assets** → **Ad accounts** → tick the ad account → **Save**. It may show only on the
   app's **Connected assets** tab, not on the ad account's.
3. **Check:** `adops-guard doctor meta --app 1234567890` (a Meta app id or an App Store id).
4. **Observed (Oct 2026, on one ad account; it may depend on the country):** app-install ads failed
   with `100/2446880`
   ("WhatsApp number required") until a WhatsApp number was linked to the Facebook Page (Page
   settings → **Linked accounts** → **WhatsApp**) or to the Instagram account. Opting out of
   creative features did not help.
5. A **Traffic** campaign cannot send people to `apps.apple.com` (`100/1487810`, **observed, Oct
   2026**). Use
   `OUTCOME_APP_PROMOTION` with `promoted_object` `{application_id, object_store_url}`, or a landing
   page.
6. **Instagram placements:** **observed (Oct 2026)**, videos of about 17 to 19 seconds came back
   `is_instagram_eligible=false` and a 14.8-second cut of the same video came back eligible; bitrate
   made no difference. Check before uploading: `adops-guard meta video-check --file cut.mp4`.

### Step 10. Verify with `adops-guard doctor meta`, then a first dry run

```text
$ adops-guard doctor meta --app 1234567890
adops-guard 0.1.0 doctor: read-only checks; nothing is changed on either platform

Setup
  ✓ config file /home/you/adops/adops-guard.ini (found in the working directory): valid, not writable by other users
  · state directory /home/you/adops/.adops-guard: not there yet (the first write creates it, mode 0700)

Meta
  ✓ ceilings: [meta] max_daily_budget = 50.00, max_lifetime_budget = 500.00, max_spend_cap = 1,000.00
  ✓ ffprobe 7.1 (for meta video-check --file)
  ✓ access token from $META_ACCESS_TOKEN: 200 characters (never printed)
  ✓ token user: Test User (111111111111111)
  ✓ permissions: ads_read and ads_management are granted
  ? app "Example App" (222222222222222): whether it is in Development or Live mode is not readable through the API
    check: developers.facebook.com > My Apps > (your app): the top bar must say App Mode: Live; in Development mode ad creatives fail with 100/1885183
  ✓ ad account act_123456789012345 "Example Ads": ACTIVE, USD, America/Los_Angeles
  · not prepaid (card or invoice): Meta charges the payment method as ads spend; funds added to another ad account do not count here
  ✓ account spending limit 5,000.00 USD: 123.45 USD spent, 4,876.55 USD left
  · rate tier development_access (the default for new apps, with low limits); usage 4% (adops-guard stops at 85%)
    tip: for higher limits, upgrade the 'Marketing API Access Tier' feature (older consoles: 'Ads Management Standard Access'): App dashboard > App Review > Permissions and features
  ✓ Instagram identity: 17841401234567890 through Page "Example Page"; creatives name it with instagram_user_id
  ✓ app 1234567890 can be advertised from act_123456789012345: "Example App" (222222222222222)

Result: 10 passed, 0 failed, 0 warning(s), 1 to check by hand
```

Then:

```bash
adops-guard meta whoami                                     # token user, permissions, ad account
adops-guard meta status
adops-guard meta set status 120000000000000003 PAUSED       # dry run: sends nothing
adops-guard meta set status 120000000000000003 PAUSED --validate   # dry run checked by Meta
```

---

## What a failure looks like

```text
$ adops-guard doctor
...
Google Ads
  ✓ ceiling: [google] max_daily_budget = 50.00 per budget
  ✓ credentials from /home/you/google-ads.yaml (mode 0600, owned by you): client_id, client_secret and refresh_token are set
  ✗ OAuth token refresh failed: HTTP 400 invalid_grant Token has been expired or revoked.
    fix: the refresh token was revoked or has expired; refresh tokens of OAuth apps in 'Testing' status expire after 7 days: publish the app (Google Auth Platform > Audience > Publish app; older consoles: OAuth consent screen), then create a new refresh token and copy it into google-ads.yaml (docs/setup.md, Google steps 4 and 5)

Meta
  ...
  ✓ token user: Test User (111111111111111)
  ✗ permissions missing: ads_management
    fix: App dashboard > Use cases > 'Create & manage ads with Marketing API' > Customize: add ads_management and ads_read (one by one if needed); then Graph API Explorer > Generate Access Token with them ticked, extend it and store it again (docs/setup.md, Meta steps 1 to 6)
...
Result: 10 passed, 2 failed, 0 warning(s), 1 to check by hand
Fix what failed, then run adops-guard doctor again.
Every error and its fix: https://github.com/deegitech/adops-guard/blob/v0.1.0/docs/troubleshooting.md
The docs/ files named above: https://github.com/deegitech/adops-guard/tree/v0.1.0/docs
```

The doctor tells a platform problem from a network one: a connection error or an HTTP 5xx is
reported as "a network or service problem, not your setup", never as a bad token or a missing
permission.

In a terminal the marks are ✓ (pass), ✗ (fail), `!` (warning), `?` (check by hand), `·`
(information) and `–` (skipped). In logs and pipes they are plain ASCII: `ok`, `FAIL`, `WARN`,
`manual`, `info`, `skip`. `--json` prints one document with every check, its status and its fix.
The exit code is 0 when nothing failed and 1 otherwise, so `adops-guard doctor` also works as a CI
or cron check.

## Token expiry and renewal

| Credential | Lifetime | Renew with |
|---|---|---|
| Meta user token (Graph API Explorer, extended) | About 60 days. It ends early after a password change, when the user removes the app, or after a security checkpoint. | Meta steps 4 to 6 again (`-U` replaces the Keychain item). |
| Meta system user token | 60 days or no expiry, as chosen when it is generated. | Business settings → System users → **Generate new token**. |
| Google refresh token | No fixed lifetime once the OAuth app is **In production**; 7 days while it is in **Testing**. It also stops working when access is revoked, after 6 months without use, or when the same user and client hold more than 100 refresh tokens (the oldest one goes). | Google steps 4 and 5 again. |
| Google access token | One hour; adops-guard refreshes it in memory on every run. | Nothing to do. |

- adops-guard cannot show the Meta token's expiry: Meta's `debug_token` endpoint needs the token in
  the URL, and adops-guard never puts a token in a URL. The Access Token Debugger in your browser
  shows it.
- Put the renewal in your calendar: about day 50 for a Meta user token.
- After renewing, run `adops-guard doctor`. An expired Meta token shows up as `190/463`, an expired
  or revoked Google refresh token as `invalid_grant`.

## Running on a server

A laptop scheduler stops when the lid closes; run `spend-watch` and cron jobs on a server. The
[systemd unit](../examples/systemd/adops-guard-spend-watch.service), the
[launchd agent](../examples/launchd/com.example.adops-guard.spend-watch.plist) and the
[crontab](../examples/crontab.example) examples carry no credentials: the config file points at
them.

- **Files:** the same 0600 files as above, owned by the user the service runs as.
- **Meta token from AWS SSM, read on every run:** create a `SecureString` parameter from a private
  JSON file, so the token is never on a command line:

  ```bash
  read -rs TOKEN                           # paste the token (it is not shown), press Enter
  (umask 077 && printf '{"Name": "/adops-guard/meta-token", "Type": "SecureString", "Value": "%s"}' \
    "$TOKEN" > ~/meta-param.json)
  unset TOKEN
  aws ssm put-parameter --cli-input-json "file://$HOME/meta-param.json" --overwrite
  rm ~/meta-param.json
  ```

  Then uncomment the three `ssm` lines in the `[meta]` section of the config file:

  ```ini
  token_source = ssm
  ssm_parameter = /adops-guard/meta-token
  ssm_region = us-east-1
  ```

  The server's AWS identity needs `ssm:GetParameter` on the parameter and `kms:Decrypt` on its key.
- **Or fetch secrets into memory at boot:** a systemd `ExecStartPre=` (with `UMask=0077`) can write
  the token, or a whole `google-ads.yaml` stored as a `SecureString`, into the service's runtime
  directory (tmpfs, e.g. `/run/user/1000/adops-guard/`). Then point `token_file` or
  `credentials_file` there. Nothing secret touches the disk.
- Run `adops-guard doctor` on the server once, as the service user, before you enable the unit.

## Common mistakes

> **Common mistakes**
>
> - **Pasting a second `[google]` or `[meta]` block into the config file.** Each section and key
>   may appear only once; edit (uncomment) the lines that are already there.
> - **Copying the example `google-ads.yaml` over an existing one.** Google's client libraries use
>   the same file: add the three keys to it instead (Google step 5 does).
> - **Storing the Meta token with `security ... -w` and no value.** The prompt cuts it at 128
>   characters. Use `-w "$(pbpaste)"`.
> - **Pasting the command and the token as two lines at once.** The command text gets stored as the
>   token (the doctor reports a token "containing whitespace").
> - **Ticking only the Page or only the Instagram account** in the Graph API Explorer dialog. Tick
>   both.
> - **Picking the "Create & manage app ads" use case.** It does not offer `ads_management` or
>   `ads_read`; use "Create & manage ads with Marketing API".
> - **Leaving the Meta app in Development mode.** Ad creatives fail with `100/1885183`.
> - **Leaving the Google OAuth app in Testing.** The refresh token dies after 7 days
>   (`invalid_grant`).
> - **Waiting for Explorer access without applying.** A new Cloud project has Test access, and real
>   accounts answer `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` (Google step 2).
> - **Generating the Meta token before the user has access to the ad account.** Give access first
>   (step 8.1), or generate and store the token again afterwards.
> - **Copying `client_id` and `client_secret` from two different OAuth clients** (`invalid_client`).
> - **Putting a manager (MCC) account in `customer_id`.** Use the client account, and the manager
>   in `login_customer_id`.
> - **A credentials file other users can read.** It is refused: `chmod 600`.
> - **No ceilings in the config file.** Every spend increase is refused until you set them.
> - **Leaving Google's auto-apply on**, or **clicking Ads Manager's "top posts" boxes**: both change
>   your account behind the tool's back.
> - **Clicking your own ads**, or asking friends to: invalid clicks that put the account at risk.
> - **Expecting a daily budget to be a cap.** Use the account budget (Google), the account
>   spending limit and campaign spend caps (Meta).
