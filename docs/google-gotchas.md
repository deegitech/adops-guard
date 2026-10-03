# Google Ads API gotchas

Things we ran into while running small Demand Gen campaigns through the Google Ads API over REST
(API v25, September and October 2026). Entries marked **observed** are what we saw at the time, not
documented rules; entries marked **documented** point at Google's own rules. Check them against the
current release notes before relying on them. Every known error, with its fix, is in
[troubleshooting.md](troubleshooting.md); the setup steps are in [setup.md](setup.md).

---

### Developer tokens were sunset; access now belongs to the Cloud project

**Documented: September 2026.** Google sunset developer tokens on 9 September 2026. API access (Test,
Explorer, Basic or Standard) is now managed on the Google Ads API Overview page of your Google Cloud
project, the `developer-token` header is optional and ignored, and Google says it will reject it in a
future major version ([developer token](https://developers.google.com/google-ads/api/docs/get-started/dev-token),
[access levels](https://developers.google.com/google-ads/api/docs/api-policy/access-levels)).
adops-guard sends a developer token only if you configure one.

### The billing counter lags behind real-time spend

**Observed: October 2026.** On an account with monthly invoicing, `account_budget.amount_served_micros`
stayed frozen for several hours while the sum of `metrics.cost_micros` kept growing. Anything that
must react to spend in time should read both and use the larger one; that is what
`adops-guard google spend-watch` does by default (`--source max`). The two count from different
starts, though: the billing counter from its account budget's start date. Compare them only over the
same window (spend-watch uses the metrics alone when you pass `--since`).

### A daily budget is not a daily cap

**Documented.** Google may spend up to twice the average daily budget on a single day, and balances it
over the month. For a hard ceiling use an account budget (monthly invoicing), and watch spend.

### `LAST_N_DAYS` ranges leave out today

**Documented.** Predefined ranges such as `DURING LAST_7_DAYS` end yesterday. adops-guard always sends
explicit `BETWEEN 'from' AND 'to'` ranges that end today in the **account's** time zone.

### Zero values are missing from REST responses

**Observed.** The REST/JSON responses leave out fields whose value is the default (`0`, `false`,
empty). A campaign with no clicks has no `metrics.clicks` key at all, and int64 values arrive as
strings. Read every metric with a default.

### `validateOnly` success is an empty object

**Observed: September 2026.** A successful `:mutate` with `"validateOnly": true` returns `{}`.

### Demand Gen: device targeting cannot be removed, only bid down

**Observed: September 2026.** Demand Gen campaigns came with device criteria that could not be
removed. Setting `bid_modifier` to `0` (minus 100 %) on desktop and TV excluded them.

### Demand Gen: gender goes through an audience

**Observed: September 2026.** Demand Gen is "audience grouped": a plain gender criterion on the ad
group was not accepted. Gender (and age) targeting went into an `Audience` with `dimensions`, attached
to the ad group as an audience criterion.

### Demand Gen channel controls: use leaf field masks

**Observed: October 2026, API v25.** Turning off YouTube in-stream for an ad group validated and
applied with the leaf mask
`demand_gen_ad_group_settings.channel_controls.selected_channels.youtube_in_stream` and only that
field in the body. `adops-guard google channel-controls` sends one leaf mask per channel it changes,
so the other channels are never touched.

### One website per ad group

**Observed: October 2026.** An ad pointing at `apps.apple.com` and an ad pointing at a separate
website could not live in the same ad group (`ONE_WEBSITE_PER_AD_GROUP`). The website ad needed its
own ad group.

### Website conversion actions: `DOWNLOAD` was refused, `OUTBOUND_CLICK` worked

**Observed: October 2026, API v25.** Creating a `WEBPAGE` conversion action with `category: DOWNLOAD`
failed with `fieldError: INVALID_VALUE` on the category. The same action with
`category: OUTBOUND_CLICK` was created. That is the default of
`adops-guard google conversion-action create`.

### Conversion action updates: transient 500s

**Observed: October 2026.** Updating `primary_for_goal` on a conversion action returned
`500 INTERNAL` twice before it went through. adops-guard retries reads and validate-only calls, but
never a real write: a 5xx during a write is reported as "may or may not have been applied", and
re-running the command reads the current value first.

### The access level sets a daily operations quota

**Documented.** Each API access level has a daily operations quota (Explorer access, for example,
allows far fewer operations a day on production accounts than Basic access); when it is used up, the
API answers `429 RESOURCE_EXHAUSTED` until the quota resets. See
[access levels](https://developers.google.com/google-ads/api/docs/api-policy/access-levels).

### Refresh tokens of "Testing" OAuth apps expire after 7 days

**Documented.** If your OAuth consent screen is in *Testing* status, refresh tokens stop working after
a week (`invalid_grant`). Publish the consent screen (or use an internal app in Workspace) for tokens
that last.

### API versions are retired

**Documented.** Each Google Ads API version is sunset roughly a year after its release. adops-guard
pins `v25` (current in October 2026); set `api_version` in the config to move, or `auto` to probe from
the newest version down.

### Accidental clicks on YouTube in-stream

**Observed: September and October 2026.** On small click-optimised Demand Gen campaigns, YouTube
in-stream took most of the clicks, much of it on full-episode TV drama and children's channels, with
CTR rising day by day while CPC fell. Clicks like these often do not convert. See
[click-quality-audit.md](click-quality-audit.md) for the checks that grew out of this.
