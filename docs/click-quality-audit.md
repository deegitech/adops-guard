# Click-quality audit

```
adops-guard google audit [--campaign ID] [--days 14 | --from YYYY-MM-DD --to YYYY-MM-DD]
                         [--top 10] [--fail-on-signal] [--json]
```

A read-only report that looks for signs that clicks are accidental or low-intent. It is a
**heuristic**: every signal is a reason to look closer, none of them is proof of invalid traffic, and
Google already filters what it considers invalid clicks before you pay for them. The question the
audit helps with is a different one: *are the clicks you pay for coming from people who meant to
click?*

## Why it exists

Observed (September and October 2026) on small click-optimised Demand Gen campaigns: YouTube in-stream
took most of the clicks, much of it on full-episode TV drama and children's channels, with CTR rising
day after day while CPC fell. On a dashboard, that looks like success. Clicks like these often do not
convert. The four checks below are designed to catch that pattern early.

## What it reads

Each breakdown is a separate GAQL query over the same date range (default: the last 14 days,
including today, in the account's time zone), for one campaign or the whole account:

| Breakdown | Query | Notes |
|---|---|---|
| Channel / ad format | `segments.ad_network_type`, `segments.ad_format_type` FROM `campaign` | Falls back to the format alone, then to the network alone, if the API rejects the combination. |
| Placements | `group_placement_view` (channels), `detail_placement_view` (videos) | Rows of the same placement are merged. Google may aggregate or omit small placements. |
| Hour of day | `segments.hour` FROM `campaign` | Account time zone. |
| Age | `ad_group_criterion.age_range.type` FROM `age_range_view` | Shown for context. |
| Day | `segments.date` FROM `campaign` | For the trend check. |

Which segments are available depends on the API version and the campaign type. When a query is
refused, the audit prints `unavailable: ...` with Google's error and marks the related signal `n/a`
instead of failing.

## The signals

| Signal | Fires when (defaults) | Why it matters |
|---|---|---|
| **In-stream CTR far above Shorts / in-feed** | in-stream CTR is at least `instream_ctr_ratio` (3.0x) the CTR of in-feed + Shorts, with at least `min_clicks` (50) in-stream clicks and `min_impressions` (1,000) other YouTube impressions | The same creative should not be several times more "interesting" in a skippable pre-roll than in a feed. A big gap often means taps on the ad while trying to skip it or to get back to the video. |
| **Clicks from TV-drama or kids channels** | at least `flagged_placement_share` (25%) of channel clicks, or of video clicks, come from placements whose names match TV-drama or kids words | Long full-episode videos and children's content are watched with phones handed around and screens tapped casually. |
| **Night-time share of clicks** | at least `night_click_share` (40%) of clicks fall in `night_hours` (00:00-06:00) **and** the night CTR is at least `night_ctr_ratio` (1.5x) the daytime CTR | Cheap night inventory with high CTR is a common pattern of low-intent clicks. |
| **CTR rising while CPC falls** | over at least `min_days` (6) days with impressions, CTR in the last third of the period is up by `ctr_rise` (30%) or more and CPC is down by `cpc_fall` (20%) or more against the first third | Click-optimised bidding drifts toward whatever inventory clicks most cheaply. That is not the same as inventory that converts. |
| *Clicks from viewers of unknown age* (info) | at least `age_undetermined_share` (40%) of clicks come from `AGE_RANGE_UNDETERMINED` | Signed-out viewers and shared devices. Informational only; it does not count toward the verdict. |

The verdict counts the four main signals that could be checked. `--fail-on-signal` makes the command
exit with code 3 when any of them fires, so a cron job or a CI step can alert you.

All thresholds live in the `[audit]` section of the config file; see
[`examples/adops-guard.example.ini`](../examples/adops-guard.example.ini).

### Keyword matching is crude on purpose

Placement names are matched against short word lists with word boundaries (so `kids` matches
"Fun Kids Songs" but not "Kidney health"). The built-in lists are English, Spanish and Portuguese:

- kids: kids, kid, children, child, toddler(s), baby, babies, nursery, rhymes, cartoon(s), preschool,
  toys, infantil(es), niños, crianças, desenho(s), dibujos animados
- TV drama: episode(s), full episode, ep, season, drama(s), series, telenovela, novela(s), capítulo,
  capitulo, episodio, episódio, temporada

Add words for your market with `extra_kids_keywords` and `extra_drama_keywords`. Expect false positives
and misses, and look at the listed placements yourself before you exclude anything.

## What to do when signals fire

1. Compare **conversions** per channel (installs, sign-ups, purchases), not clicks. If you have no
   conversion tracking yet, the landing-page snippet and
   `adops-guard google conversion-action create` are a cheap start.
2. If in-stream is the problem, turn it off for the affected Demand Gen ad groups and watch a few days:

   ```
   adops-guard google channel-controls --ad-group <AD_GROUP_ID> --youtube-in-stream off          # dry run
   adops-guard google channel-controls --ad-group <AD_GROUP_ID> --youtube-in-stream off --apply
   ```

3. Exclude placements or tighten content suitability in Google Ads. adops-guard does not edit
   exclusions.
4. Consider audience signals (people interested in your category) and, for night traffic, an ad
   schedule.

## Example

The numbers below come from the test fixtures, not from a real account.

```
$ adops-guard google audit --campaign 11111111111
Click-quality audit  123-456-7890  campaign 11111111111  2030-04-20 .. 2030-05-03  (America/New_York, USD)
Heuristic: a fired signal is a reason to look closer, not proof of accidental or invalid clicks.

Channels (segments.ad_network_type, segments.ad_format_type)
  channel            impressions  clicks  click share    CTR   CPC    cost
  youtube in-stream       50,000   2,000        85.1%  4.00%  0.20  400.00
  youtube shorts          40,000     300        12.8%  0.75%  0.50  150.00
  discover                 5,000      50         2.1%  1.00%  0.50   25.00
...
Signals
  [FIRED] In-stream CTR far above Shorts / in-feed
          in-stream CTR 4.00% vs 0.75% on in-feed + Shorts (5.3x; threshold 3.0x); in-stream has 85.1% of all clicks
          -> turn YouTube in-stream off for the affected ad groups and judge by conversions, not clicks: ...
  [FIRED] Clicks from TV-drama or kids channels
          on placements whose names look like TV drama or kids content: 71.4% of channel clicks (1,000 of 1,400) ...
  [ok   ] Night-time share of clicks
          25.0% of clicks between 00:00 and 06:00 (account time zone), night CTR 2.00% vs day 2.00% (1.0x); ...
  [FIRED] CTR rising while CPC falls
          first 3 days vs last 3 days: CTR 0.90% -> 3.30% (+267%), CPC 0.90 -> 0.43 (-52%); ...

Verdict: 3 of 4 signals point to low-intent or accidental clicks. Check conversions per channel before spending more.
```

`--json` prints every breakdown, the placements with their flags, and each signal with its status and
detail, for your own dashboards.
