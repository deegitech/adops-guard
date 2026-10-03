# Meta Marketing API gotchas

Things that cost us time while running app-install and traffic campaigns through the Marketing API
(Graph API v26.0) for an iOS game. Each entry says **when we observed it**. None of this is official
documentation; Meta changes behaviour between API versions and sometimes per account, so treat every
entry as "worked for us at the time" and check it against the current changelog.

Error codes are written as `code/error_subcode`, the way they appear in Graph API error objects.
adops-guard prints a hint when it sees one of these subcodes. Every known error, with its fix, is in
[troubleshooting.md](troubleshooting.md); the setup steps are in [setup.md](setup.md).

---

## 1. A Traffic campaign cannot send people to an App Store URL

**Observed: October 2026, API v26.0.** Error `100/1487810`.

A campaign with `objective: OUTCOME_TRAFFIC` whose ads link to `https://apps.apple.com/...` was
rejected. Meta's message (we received it localised) said, in effect, that app URLs are only supported
with the app-installs objective. It failed the same way with each call to action we tried:
`INSTALL_MOBILE_APP`, `DOWNLOAD` and `LEARN_MORE`.

What worked: a new campaign with `objective: OUTCOME_APP_PROMOTION` (see the next entry for what it
needs). The alternative is to keep Traffic and send people to your own landing page with an App Store
button; [`snippets/landing-consent-conversion.html`](../snippets/landing-consent-conversion.html) keeps
per-campaign App Store attribution working on that page.

## 2. App Promotion needs the iOS platform on the Meta app, and the app linked to the ad account

**Observed: October 2026, API v26.0.**

Before `OUTCOME_APP_PROMOTION` ad sets validated, two things had to be done by hand in Meta's UIs:

1. In the Meta app's settings (developers.facebook.com, *Settings > Basic*), add the **iOS** platform
   with the App Store ID of your app.
2. In Business settings, give the **ad account** access to that app (*Accounts > Apps > your app >
   assign the ad account*).

A quick check from the API: the app must appear in

```
GET act_<AD_ACCOUNT_ID>/advertisable_applications?fields=id,name,object_store_urls,supported_platforms
```

The ad set then carries `promoted_object = {"application_id": "<app id>", "object_store_url":
"https://apps.apple.com/app/id<APP_STORE_ID>"}`. Without any SDK in the app, an ad set with
`optimization_goal: LINK_CLICKS` was accepted.

## 3. "WhatsApp number required" on app-install ads

**Observed: October 2026, API v26.0.** Error `100/2446880`.

Creating video ads in an App Promotion campaign failed with a user message saying a WhatsApp number
must be connected to the Facebook Page or Instagram account before the ad can run. The ads had no
WhatsApp destination or call to action.

What did **not** help: opting out of Meta's automatic creative features one by one through
`degrees_of_freedom_spec.creative_features_spec`, and putting the application id into the call to
action value.

What did help: connecting a WhatsApp number to the Page (and Instagram account) used by the ads.
After that, the same requests validated. We found no documentation explaining why app-install ads
needed it.

## 4. The Instagram position `explore` became `explore_home`

**Observed: September 2026, API v26.0.** Error `100/2490589`.

`targeting.instagram_positions` with `"explore"` was rejected: the message said the Instagram Explore
placement is deprecated for this API version and cannot be selected. `"explore_home"` was accepted.

## 5. `instagram_actor_id` became `instagram_user_id`

**Observed: September 2026, API v26.0.**

Creatives that named the Instagram identity with `instagram_actor_id` in `object_story_spec` were
rejected. The same creative with `instagram_user_id` (the Instagram account's user id) was accepted.

## 6. `is_adset_budget_sharing_enabled` is required

**Observed: September 2026, API v26.0.** Error `100/4834011`.

Creating a campaign without a campaign budget (budgets on the ad sets) failed until the request
included `is_adset_budget_sharing_enabled`. We sent `false`, so ad sets do not lend each other part
of their budgets.

---

## More things we ran into

### HTTP 200 can carry an error

**Observed: October 2026.** Code `31`, subcode `3858385`: "This request requires the user to take a
pending action". Meta wanted the account owner to complete a security check in Ads Manager, and
answered `POST .../ads` with **HTTP 200** and an `error` object in the body. Existing ads kept
running; nothing could be created or edited until the check was done. adops-guard treats any
response with an `error` object as an error, whatever the status code.

### Ads from an app in Development mode are rejected

**Observed: October 2026.** Error `100/1885183`. Creatives made with a token of a Meta app that was
still in *Development* mode were rejected. Switching the app to *Live* (which wanted a privacy policy
URL, a category and an icon) fixed it.

### Videos of about 17 to 19 seconds were not eligible for Instagram

**Observed: October 2026.** Videos between about 17 and 19 seconds came back with
`is_instagram_eligible: false`. Re-encoding them at a lower bitrate did not change that. A 14.8 second
cut of the same video came back `true`. We did not find this limit documented, and we did not test
lengths between 14.8 and 17 seconds, so a limit of 15 seconds is an assumption.
`adops-guard meta video-check` reads the flag for uploaded videos and, before you upload, flags local
files of 15 seconds or longer (`--max-seconds`) using `ffprobe`.

### `standard_enhancements` can no longer be set on a creative

**Observed: October 2026.** Error `100/3858504`: the message asked to set creative features one by
one instead.

### `?ids=` multi-object reads failed

**Observed: October 2026, API v26.0.** `GET /?ids=<a>,<b>` returned HTTP 500 with a message saying
the `ids` parameter is deprecated from v26.0. Read objects one at a time.

### Creative names come back with a suffix

**Observed: October 2026.** Meta appended a date and a hex string to the creative names we sent
(`My creative` came back as `My creative YYYY-MM-DD-<hex>`); ad names and video titles came back
unchanged. Do not find creatives by exact name.

### The Marketing API permissions come from the app's use case

**Observed: September 2026.** `ads_management` and `ads_read` only became available to the token
after the Meta app had the "Create & manage ads with Marketing API" use case; the app-ads use case
we started with did not offer them.

### Validate-only on updates: not verified by us

**Not yet observed.** Meta documents `execution_options=["validate_only"]` as checking a request
without applying it. We relied on it live only when creating campaigns, ad sets and ads, never for
updates of existing objects (status, budgets, spend caps). Until that is confirmed, a Meta dry run in
adops-guard sends nothing unless you pass `--validate`, and a dry run that does send it reads the
object before and after and stops (exit 4) if anything changed. If you try it on a paused test
object, please report what you see.

### A daily budget is not a daily cap

Meta's help pages say spend can go above a daily budget on some days (by up to 75%) while averaging
out over the week. For a hard ceiling use a campaign spend cap (`adops-guard meta spend-cap`) or the
account spending limit in Ads Manager.
