# Troubleshooting

Every error adops-guard knows, what it means, and the fix. Start with `adops-guard doctor`: it runs
the read-only checks in order and prints the fix under each failed one. Other commands end an error
with a line such as `error: hint: ...`: that is the one-line version of the fix in this table.

How to find an entry: search this page for the error code or a few words of the message.

- **Meta** errors are written `code/subcode`, the way the Graph API error object has them
  (`"code": 100, "error_subcode": 1885183` is `100/1885183`). adops-guard prints them as
  `code 100/1885183` in the error line.
- **Google** errors are written by their error code (`USER_PERMISSION_DENIED`), with the HTTP status.
- **Observed** marks behaviour we saw (with the month) that is not in the platforms' documentation.
  Console menus get renamed often: names may differ.

Setup steps referenced below are in [setup.md](setup.md).

## Contents

- [adops-guard's own messages](#adops-guards-own-messages)
- [Google OAuth (refreshing the token)](#google-oauth-refreshing-the-token)
- [Google Ads API errors](#google-ads-api-errors)
- [Google Ads: no error, but something is wrong](#google-ads-no-error-but-something-is-wrong)
- [Meta Graph and Marketing API errors](#meta-graph-and-marketing-api-errors)
- [Meta: no error, but something is wrong](#meta-no-error-but-something-is-wrong)

## adops-guard's own messages

### Config file and state directory

| Message | Meaning | Fix |
|---|---|---|
| `config file not found: PATH` | `--config` or `$ADOPS_GUARD_CONFIG` names a file that does not exist. | Check the path, or create the file from [the example](../examples/adops-guard.example.ini). |
| `cannot read PATH: ... section 'meta' already exists` / `option 'token_source' in section 'meta' already exists` | A second `[meta]` (or `[google]`) block was pasted below the copied example, or a key appears twice. Each section and each key may appear only once. | Merge the blocks: edit the lines already in the file (the example has them commented out with `#`) instead of pasting a new block. Do not start over from the example: that loses your edits. |
| `cannot read PATH: File contains no section headers` / `... parsing errors` | The file is not valid INI. | Fix the line it names: `[section]` headers and `key = value` lines, as in [the example](../examples/adops-guard.example.ini). |
| `config file PATH can be changed by other users (mode 0o664)` | The config decides your ceilings and where tokens come from, so it must be yours alone. | `chmod go-w PATH` |
| `config file PATH belongs to another user` | Same reason. | Use a config file you own. |
| `unknown key [google] max_daily_budjet` / `unknown section [googel]` | A typo. It is refused, so a misspelled ceiling can never pass silently. | Fix the name; the hint lists the known keys. |
| `[google] developer_token looks like a secret; secrets never go in the config file` | Keys that look like secrets are refused in the config file. | Move the value to an environment variable, a 0600 file, the Keychain or SSM ([setup.md](setup.md)). |
| `[google] api_base_url ... is only for the offline test suite` | A test-only endpoint key; credentials would be sent there. | Remove it. `ADOPS_GUARD_TEST_ENDPOINTS=1` belongs in the test suite only. |
| `refusing to send credentials to HOST` / `refusing a non-HTTPS endpoint` | Tokens only go to `googleads.googleapis.com`, `oauth2.googleapis.com` and `graph.facebook.com` over HTTPS. | Remove the endpoint override. |
| `state directory PATH can be changed by other users` | Someone else could plant a symlink there and redirect a write. | `chmod 700 PATH` |
| `state directory PATH is a symlink` / `belongs to another user` | Same reason. | Point `state_dir` or `--state-dir` at a real directory you own. |
| `state file PATH/state.json is not valid JSON` | The idempotency file was damaged. | Move it aside and re-run; `journal.jsonl` keeps the full history. |
| `an earlier --apply for this target did not finish` (warning) | A previous write ended ambiguously (timeout, HTTP 5xx). The doctor lists these as "did not finish". | Re-run the same command: it reads the current value first and records whether the earlier write is in place. |

### Credentials

| Message | Meaning | Fix |
|---|---|---|
| `no Google Ads credentials: ~/google-ads.yaml does not exist and GOOGLE_ADS_* variables are not set` | No Google credentials anywhere. | Google steps 1 to 5 (Cloud project, access level, OAuth client, refresh token, `google-ads.yaml`). Using only Meta? Delete the `[google]` section of the config file and the doctor skips Google. |
| `some GOOGLE_ADS_* variables are set but these are missing: ...` | Environment variables are all or nothing. | Set `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET` and `GOOGLE_ADS_REFRESH_TOKEN`, or unset them all to use the file. |
| `PATH is missing: client_id, client_secret, refresh_token` | The example's `REPLACE_ME` placeholders are still there. | Google steps 3 to 5. |
| `PATH can be read by other users (mode 0o644)` | A credentials or token file must be private. | `chmod 600 PATH` |
| `PATH belongs to another user` | Same. | Credential files must be owned by the user running adops-guard. |
| `service-account credentials are not supported yet` | `google-ads.yaml` has `json_key_file_path`. | Use an OAuth refresh token (Google steps 3 to 5). |
| `no Meta access token: META_ACCESS_TOKEN is not set and no token source is configured` | No token anywhere. | No token yet: Meta steps 1 to 6. Have one: `read -rs META_ACCESS_TOKEN && export META_ACCESS_TOKEN` (paste, Enter), or set `[meta] token_source` (Meta step 6). |
| `META_ACCESS_TOKEN is not set ([meta] token_source = env)` | The config reads the token from the environment, and this shell has none. | The same as above. Stored it in the Keychain or a file? Set `token_source = keychain` or `file` in the existing `[meta]` section. |
| `[meta] token_source = file needs [meta] token_file or META_ACCESS_TOKEN_FILE` | | Add `token_file = ~/.config/adops-guard/meta-token` under `[meta]` (Meta step 6). |
| `token file not found: PATH` | | Check `[meta] token_file` (or `$META_ACCESS_TOKEN_FILE`), or create the file (Meta step 6). |
| `token_source = keychain needs [meta] keychain_service` | | Add `keychain_service = adops-guard-meta` under `[meta]`: the `-s` name the token was stored with. |
| `token_source = ssm needs [meta] ssm_parameter` | | Add `ssm_parameter = /adops-guard/meta-token` (the full name, with its leading `/`) under `[meta]`. |
| `the Meta access token from ... contains whitespace` | A token has no spaces. Usually the command text was stored (two lines pasted at once). | Store it again: paste the command first, then copy the token, then press Enter (Meta step 6). |
| `the Meta access token from ... is empty` | The variable, file or Keychain item is empty. | Store it again (Meta step 6). |
| `could not read the token from the macOS Keychain (exit 44)` | No Keychain item with that service (and account). | Check `[meta] keychain_service` and `keychain_account`, or store it (Meta step 6). |
| `could not read the token from the macOS Keychain (exit N)`, another N | The Keychain refused: it is locked, or `security` may not read the item. | Check `keychain_service` and `keychain_account`, unlock the Keychain, and click **Allow** if macOS asks. |
| `reading the token from the macOS Keychain timed out after 30 s` | The Keychain is locked or waits for a permission prompt. | Unlock it and click **Allow** (or **Always Allow**) for `security`. |
| `token_source = keychain works only on macOS` | | Use `file`, `env` or `ssm` elsewhere (Meta step 6, [Running on a server](setup.md#running-on-a-server)). |
| `'aws' was not found; it is needed to read the token from AWS SSM` | The AWS CLI is missing. | Install the AWS CLI v2, or use another `token_source`. |
| `could not read the token from AWS SSM (exit 254): ... ParameterNotFound` | Wrong name or region. | Check `[meta] ssm_parameter` (the full name, with its leading `/`) and `ssm_region`. |
| `... AccessDeniedException` (SSM) | The AWS identity may not read it. | Allow `ssm:GetParameter` on the parameter and `kms:Decrypt` on its key. |
| `... Unable to locate credentials` (SSM) | The AWS CLI found no AWS credentials. | Set `AWS_PROFILE`, or run on a host with an instance role. |
| doctor: `the token is exactly 128 characters` (after Meta refused it) | The macOS Keychain prompt cuts a value at 128 characters; Meta tokens are about 200. | Store it again with `security add-generic-password -U -a "$USER" -s adops-guard-meta -w "$(pbpaste)"` (Meta step 6). |
| doctor: `this does not look like a Facebook Login token (those start with EAA)` | An Instagram Login token or something else was stored. | Generate a token in Graph API Explorer (Meta step 4). |

### Settings and guards

| Message | Meaning | Fix |
|---|---|---|
| `no Google Ads customer id` | | Set `[google] customer_id` or pass `--customer-id`; `adops-guard google accounts` lists yours. |
| `customer id must have 10 digits, like 123-456-7890` | | Copy the ID shown next to the account name in Google Ads. |
| `no Meta ad account configured` | | Set `[meta] ad_account_id = act_...` or pass `--ad-account`. |
| `ad account ids look like act_123456789012345` | | Copy the ID from the account menu in Ads Manager. |
| `no built-in Meta currency offset for XYZ` | Meta stores money in minor units; this currency is not in the built-in table. | Check Meta's currency table and set `[meta] currency_offset` (usually 100). |
| `refusing to increase spending: no ceiling is configured ([google] max_daily_budget)` | Fail closed: without a ceiling every increase is refused. | Set the ceiling in the config file, or pass `--override-limit` for this one change. |
| `... is above the configured ceiling ...` | | Lower the amount, raise the ceiling, or pass `--override-limit`. |
| `the budget of campaign ... is shared by N campaigns` | Changing it changes all of them. | Pass `--shared-ok` if that is what you want. |
| `... belongs to ad account act_..., not the configured act_...; nothing was changed` | Writes are limited to the configured ad account. | Use the right ad account, or the right object ID. |
| `... is below this account's minimum daily budget` / `minimum campaign spend cap` | Meta would refuse it. | Use a larger amount. |
| `removing the spend cap of campaign ... removes a hard spending limit` | | Pass `--override-limit` if you really want no cap. |
| `another adops-guard write is running` | The single-writer lock is held. | Wait for it to finish; the lock file names the command. |
| `no start date to count spend from` (`spend-watch`) | No active account budget (automatic payments): the watch refuses to count all-time spend. | Pass `--since YYYY-MM-DD`, the first day to count, in the account's time zone. |
| `--since ... is in the future` | | Use a past day (account time zone). |
| `Meta API usage is at N% (this tool stops at 85%); no request was sent` | adops-guard's usage guard (exit 5). | Wait: the window is rolling (up to an hour). `adops-guard meta usage` shows the headers. |
| `read back X, expected Y` (exit 4) | The value after the write is not what was sent. | Check the object in the platform's UI; `journal.jsonl` has the request and the response. |
| `the object changed during a validate-only request` (exit 4) | A dry run's validate-only request, or someone else, changed the object. | Check it now, and please report it ([SECURITY.md](../SECURITY.md)). |
| `...; the write may or may not have been applied` | A timeout or HTTP 5xx during a write. Writes are never retried blindly. | Re-run the same command: it reads the current value first and only writes if needed. |

### Network and service problems

| Message | Meaning | Fix |
|---|---|---|
| `network error on GET https://...: ...` (connection refused, timed out, name not resolved) | adops-guard could not reach the API. Not a setup problem. | Check the connection or the proxy (`HTTPS_PROXY`), then re-run later. Reads are tried up to three times first. |
| doctor: `Meta did not answer: ...` | A network error, an HTTP 5xx or a temporary error on Meta's side (codes `1` and `2`) on the first Meta call. The token was not refused. | The same: check the connection or proxy and run the doctor again later. Only `190` and `102` mean that Meta refused the token. |
| doctor: a check fails with HTTP 500, 502 or 503 | A problem on the platform's side; reads were retried. | Run the doctor again later. If it persists, check the platform's status page. |

## Google OAuth (refreshing the token)

adops-guard exchanges the refresh token for an access token at `oauth2.googleapis.com` on every run.

| Error | Meaning | Fix |
|---|---|---|
| `invalid_grant` | The refresh token was revoked or has expired. OAuth apps left in **Testing** get refresh tokens that expire after **7 days**. Tokens also stop after 6 months unused, or when the same user and client hold more than 100 (the oldest goes). | Publish the app (Google Auth Platform → **Audience** → **Publish app**; older consoles: OAuth consent screen), then create a new refresh token (Google steps 4 and 5). |
| `invalid_client` | `client_id` or `client_secret` is wrong, or they come from different OAuth clients. | Copy both again from the same **Desktop app** client (Google Auth Platform → **Clients**, or APIs & Services → **Credentials**). |
| `unauthorized_client` | The refresh token was made with a different OAuth client. | Create a new refresh token with this client and copy it into `google-ads.yaml` (Google steps 4 and 5). |
| `the OAuth token endpoint answered HTTP 503; retry later` | Google's side is busy. Not a credentials problem. | Nothing: it is retried; re-run later if it persists. |

## Google Ads API errors

| Code (HTTP status) | Meaning | Fix |
|---|---|---|
| `SERVICE_DISABLED` (403 `PERMISSION_DENIED`, "Google Ads API has not been used in project ... or it is disabled") | The API is not enabled in the Cloud project of your OAuth client. | Cloud console → **APIs & Services** → **Library** → **Google Ads API** → **Enable** (Google step 1). |
| `ACCESS_TOKEN_SCOPE_INSUFFICIENT` (403) | The refresh token was created without the `adwords` scope. | Create a new one with `https://www.googleapis.com/auth/adwords` (Google steps 3 and 4). |
| `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` (403, an `authorizationError`) | The Google Cloud project that owns your OAuth client still has **Test** access, which reaches test accounts only. API v25 answers this when such a project calls a production account ([Google's note](https://developers.google.com/google-ads/api/docs/get-started/dev-token)). New Cloud projects start on Test, so a new setup often hits this first. | Cloud console → **Google Ads API** → **Overview** → **Upgrade access level** → **Apply for access** (Explorer). Google may grant Explorer right after you apply. Google step 2. |
| `DEVELOPER_TOKEN_NOT_APPROVED` (403) | Your Cloud project's access level does not allow this: **Test** access reaches test accounts only; **Explorer** excludes Keyword Planner, audience insights, reach planning and billing/payments services (**observed, Oct 2026**, on Keyword Planner and audience insights). | Cloud console → **Google Ads API** → **Overview** → **Upgrade access level** → **Apply for access** (Explorer; Basic after brand verification). Google step 2. |
| `RESOURCE_EXHAUSTED` (429) | The daily operations quota of your access level is used up (Explorer: 2,880 a day on production accounts). | Back off and retry after the 24-hour window (exit 5), or apply for Basic access. |
| `RESOURCE_TEMPORARILY_EXHAUSTED` (429) | Too many requests in a short time. | Wait a few minutes and re-run. |
| `USER_PERMISSION_DENIED` (403) | The OAuth user cannot reach this account directly. | If you reach it through a manager (MCC) account, set `login_customer_id`; otherwise give the user access: Google Ads → **Admin** → **Access and security**. |
| `INVALID_LOGIN_CUSTOMER_ID_SERVING_CUSTOMER_ID_COMBINATION` | `login_customer_id` is not a manager of `customer_id`. | Set it to the manager the client account is linked to, or remove it. |
| `CUSTOMER_NOT_ENABLED` | The account is cancelled, suspended or not set up yet. | Finish setup and billing in the Google Ads UI, or use another `customer_id`. |
| `CUSTOMER_NOT_FOUND` | No account with this ID. | Check the 10 digits. |
| `NOT_ADS_USER` | The Google account behind the refresh token has no Google Ads access. | Sign in to ads.google.com with it once, or create the refresh token with a user who has access (Google steps 4 and 5). |
| `ACTION_NOT_PERMITTED` | The user's role in the account is too low for this (Read only cannot change anything). On API versions before v25 it is also what a Cloud project with **Test** access gets on a production account. | Google Ads → **Admin** → **Access and security** → **Standard** or **Admin**. On a read with an older `api_version`: apply for Explorer access (Google step 2). |
| `TWO_STEP_VERIFICATION_NOT_ENROLLED` | The Google account behind the refresh token has no 2-Step Verification. | Turn it on (myaccount.google.com → **Security**), then retry. |
| `OAUTH_TOKEN_REVOKED`, `OAUTH_TOKEN_DISABLED`, `OAUTH_TOKEN_INVALID` | Google refused the OAuth grant. | Create a new refresh token (Google steps 4 and 5). |
| `UNRECOGNIZED_FIELD` | The field does not exist in the configured API version. | Set `[google] api_version` (or `auto`). The audit reports such breakdowns as unavailable instead of failing. |
| `PROHIBITED_FIELD_IN_SELECT_CLAUSE` | The field cannot be selected from this resource. | A bug report is welcome if adops-guard sent it. |
| HTTP 404 on every call | The configured API version is retired. | Set `[google] api_version = auto`, or a newer `vNN`. |
| `ONE_WEBSITE_PER_AD_GROUP` (**observed, Oct 2026**) | An ad group cannot mix `apps.apple.com` and your own website as final URLs. | Put the website ads in an ad group of their own. |
| `fieldError: INVALID_VALUE` on `category` when creating a `WEBPAGE` conversion action (**observed, Oct 2026**) | `DOWNLOAD` was refused for a website action. | Use `OUTBOUND_CLICK`, the default of `conversion-action create`. |
| `500 INTERNAL` on `validateOnly` or a mutate (**observed, Oct 2026**) | Transient. | Reads and validate-only calls are retried with backoff. A real write is reported as "may or may not have been applied": re-run the command, which reads the current value first. |

## Google Ads: no error, but something is wrong

| Symptom | Meaning | Fix |
|---|---|---|
| All campaigns stopped, although promotional credit is waiting | The **account budget**'s spending limit was reached; it stops everything. | Google Ads → **Billing** → **Account budget** → **Edit** and raise it. The doctor warns below 10% left. |
| The billing counter (`amount_served_micros`) is frozen while spend grows (**observed, Oct 2026**) | It lags real-time spend by hours. | Use the sum of `metrics.cost_micros`; `spend-watch` and the doctor use the larger of the two. |
| A "spend X, get X" promotional credit did not arrive | The offer counts the **served** amount, and refunds for invalid clicks can pull you back under the threshold. | Keep a margin above it; check the real figure with `adops-guard google report`. |
| A channel or setting you turned off is on again | Recommendations **auto-apply** changed it. | Google Ads → **Campaigns** → **Recommendations** → **Auto-apply** → turn it off. The doctor lists auto-applied types. |
| Many cheap clicks, CTR rising every day while CPC falls, almost no installs (**observed, Sep and Oct 2026**) | Accidental clicks: on Demand Gen with "Maximise clicks", YouTube in-stream took most of the clicks, with a CTR several times that of Shorts and in-feed, mostly on full-episode TV dramas and kids' channels. | `adops-guard google audit`, then `google channel-controls --ad-group ID --youtube-in-stream off`, and audience segments. See [click-quality-audit.md](click-quality-audit.md). |
| A gender criterion is refused on a Demand Gen ad group (**observed, Sep 2026**) | Demand Gen ad groups are "audience grouped". | Put gender (and age) into an Audience **dimension** and attach that to the ad group. |
| The account-default conversion goal shows **Misconfigured** (**observed, Oct 2026**) | Its only conversion action is secondary. | Make the action primary (`--primary`) and set campaign-level conversion goals (`campaign_conversion_goal.biddable`) so unrelated campaigns are not affected. |
| A daily budget was overspent | Google may spend up to twice the average daily budget on one day. | Use an account budget for a hard total, and `spend-watch`. |
| Clicks from your own team | Invalid clicks: they do not count, and they put credits and the account at risk. | Never click your own ads, and do not ask friends to. |

## Meta Graph and Marketing API errors

adops-guard treats any response with an `error` object as an error, also when the HTTP status is
**200** (**observed, Oct 2026**, with `31/3858385`).

### Access tokens and permissions

| Code | Meaning | Fix |
|---|---|---|
| `190/463` | The access token expired (user tokens last about 60 days after extending). | Generate and extend a new one, then store it again (Meta steps 4 to 6). |
| `190/467` | The token is invalid: logged out, revoked, or cut short when it was stored (the Keychain prompt keeps 128 characters). | Generate a new one and store it with `-w "$(pbpaste)"` (Meta step 6). |
| `190/460` | The token stopped working after a password change. | Generate and store a new token. |
| `190/458` | The user removed the app or never authorised it. | Generate a new token for this app. |
| `190/459` | The Facebook account is checkpointed. | Log in at facebook.com, clear the checkpoint, then generate a new token. |
| `190/464` | The Facebook account is not confirmed. | Confirm it at facebook.com, then generate a new token. |
| `190/492` | The token's user no longer has a role on the Page. | Give the role back, then generate a new token. |
| `190` (other subcodes) | The token is invalid or expired. | Generate, extend and store a new token. |
| `102` | The session behind the token is no longer valid. | Generate, extend and store a new token. |
| `10` | The app or token lacks a permission. | App dashboard → **Use cases** → **Create & manage ads with Marketing API** → **Customize**: add `ads_management` and `ads_read`; then a new token (Meta steps 2, 4 to 6). |
| `3` | "Application does not have the capability to make this API call": the app has no Marketing API use case. | The same as `10`: add the use case **Create & manage ads with Marketing API** (Meta step 1), then a new token. |
| `270` | "This Ads API request is not allowed for apps with development access level": the app is on the lowest Marketing API access tier, where the token's user must be an admin of both the app and the ad account. | Make the token's user (or system user) an admin of the app (App dashboard → **App roles**) and give it full control of the ad account, then generate a new token (Meta steps 4 to 6). Or upgrade the app's tier: App dashboard → **App Review** → **Permissions and features** → **Marketing API Access Tier** (older consoles: **Ads Management Standard Access**); names may differ. |
| `200` to `299` (other codes) | Permission error. | The token needs `ads_read` (reads) and `ads_management` (writes), and its user a role on the ad account (Business settings → **Accounts** → **Ad accounts** → **Assign people**). Check with `adops-guard doctor meta`. |
| `100/33` | The object does not exist, or the token's user cannot see it. | Check the ID; give the user or system user access to the ad account (Meta step 8). |

### Errors we hit while running campaigns

| Code | Message (paraphrased) | Fix |
|---|---|---|
| `100/1885183` (**observed, Oct 2026**) | The creative post was created by an app in development mode. | Switch the Meta app to **Live**: App settings → **Basic** needs a privacy policy URL, a category and an icon; then **App Mode: Live** (Meta step 3). |
| `31/3858385` (**observed, Oct 2026**; arrives with HTTP 200) | "Please verify your identity" / the user must take a pending action. Running ads continue; creating and editing are blocked. | Open Ads Manager and follow the banner. Observed: it sometimes cleared just by visiting; otherwise verify at facebook.com/accountquality. |
| `100/1487810` (**observed, Oct 2026**) | An App URL is only supported with the App Installs objective. | A Traffic campaign cannot link to `apps.apple.com`: use `OUTCOME_APP_PROMOTION` with `promoted_object` `{application_id, object_store_url}`, or link to your own landing page. |
| `100/2446880` (**observed, Oct 2026**, on one ad account; it may depend on the country) | A WhatsApp number is required, on app-install ads with no WhatsApp destination. | Link a WhatsApp number to the Facebook Page (Page settings → **Linked accounts** → **WhatsApp**) or to the Instagram account. Opting out of creative features did not help. |
| `100/3858504` (**observed, Oct 2026**) | `standard_enhancements` cannot be set on the creative. | Set each feature in `degrees_of_freedom_spec.creative_features_spec` instead. |
| `100/4834011` (**observed, Sep 2026**) | `is_adset_budget_sharing_enabled` is required when the budget is on the ad sets. | Set it explicitly (`false` keeps the ad set budgets separate). |
| `100/2490589` (**observed, Sep 2026**) | The Instagram `explore` placement is deprecated. | Use `explore_home` in `targeting.instagram_positions`. |
| no code (**observed, Sep 2026**) | `instagram_actor_id` rejected in `object_story_spec`. | Use `instagram_user_id`: the Instagram account's ID (`17841401234567890`; the doctor prints it). |
| HTTP 500, "the ids parameter is deprecated" (**observed, Oct 2026**) | `GET /?ids=a,b` multi-object reads failed on v26.0. | Read objects one at a time. |
| `(#100) Tried accessing nonexisting field (x)` | The field does not exist in this API version. | adops-guard drops the field and carries on; nothing to do. |

### Rate limits, blocks and versions

| Code | Meaning | Fix |
|---|---|---|
| `4`, `17`, `32`, `613`, `80000` to `80014`, HTTP 429 | Rate limits (app, user, Page, hourly, business use case). New apps are on the `development_access` tier. | Wait, then re-run (exit 5). `adops-guard meta usage` shows the headers; adops-guard stops at 85% by itself. For more, upgrade the **Marketing API Access Tier** feature (older consoles: **Ads Management Standard Access**) in App Review → **Permissions and features**. |
| `368` | Meta blocked the action for a while (policy). | Wait; check facebook.com/accountquality. |
| `2635` | This Graph API version is deprecated. | Set `[meta] api_version` to a current version (Meta's changelog lists them). |
| `1`, `2` | Unknown or temporary error on Meta's side. | Reads are retried; re-run later. Writes are never retried blindly. |

## Meta: no error, but something is wrong

| Symptom | Meaning | Fix |
|---|---|---|
| `ads_management` and `ads_read` are not offered in Graph API Explorer (**observed, Sep 2026**) | The app has the "Create & manage app ads" use case, not the Marketing API one. | Add the use case **Create & manage ads with Marketing API** (Meta step 1); add permissions one at a time. |
| The permission picker in Graph API Explorer is a text box | It sometimes is. | Type each permission name. |
| The token works for ads but not for the Page or Instagram | Only one of the two was ticked in the token dialog. | Generate a new token and tick both the Facebook Page and the Instagram account. |
| An App Promotion ad set cannot use your app ("the app isn't advertisable") (**observed, Oct 2026**) | The app has no iOS platform, or the ad account was not given access to the app. | (1) App settings → **Basic** → **Add platform** → **iOS**: bundle ID and App Store ID. (2) Business settings → **Accounts** → **Apps** → your app → **Add assets** → **Ad accounts** → tick the ad account; it may show only on the app's **Connected assets** tab. Check: `adops-guard doctor meta --app ID`. |
| A video comes back `is_instagram_eligible=false` (**observed, Oct 2026**) | Videos of about 17 to 19 s were ineligible for Instagram ad placements; a 14.8 s cut of the same video was eligible; bitrate made no difference. | Cut it under 15 s. `adops-guard meta video-check` checks uploaded videos and local files. |
| A daily budget was overspent | Meta may spend up to about 75% more than the daily budget on a single day. | Use a campaign spend cap (`adops-guard meta spend-cap`) and the account spending limit. |
| All ads stopped | The account spending limit was reached. | Ads Manager → **Billing & payments** → **Account spending limit**: raise or reset it. The doctor warns below 10% left. |
| The card was charged although you added funds | The account is not prepaid: a card-paid account is charged even if you "added funds" elsewhere, and prepaid funds cannot be moved between ad accounts. | The doctor shows prepaid or not (`is_prepay_account`). |
| New Traffic campaigns with large budgets appeared (**observed, Oct 2026**) | Ads Manager's "create an ad from your top posts" boxes create separate campaigns with large default budgets. | Pause them; do not click those boxes if you manage campaigns by API. |
| Many followers, no installs (**observed, Oct 2026**) | Follower and profile-visit campaigns did not produce installs. | Use App Promotion, or Traffic to a landing page that tracks App Store clicks. |
| Creative names come back with a date and a hex suffix (**observed, Oct 2026**) | Meta renames creatives. | Do not find creatives by exact name. |
