# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-04

First public release.

### Added

- One write path for both platforms: plan, platform-side validation, `--apply`, read-back,
  journal (`journal.jsonl`), idempotency state (`state.json`) and a single-writer lock.
- A dry run that sends a validate-only request reads the object before and after it and stops
  (exit 4, journal `dry-run-wrote`) if anything changed. Meta dry runs send nothing unless
  `--validate` is given; `--apply` always validates first.
- Spend ceilings (`max_daily_budget`, `max_lifetime_budget`, `max_spend_cap`) that fail closed, with
  `--override-limit` for a single command.
- Credentials from environment variables, chmod 600 files, the macOS Keychain or AWS SSM; a redactor
  for all output, errors, the journal and the state file; an endpoint allowlist; no redirects.
  Test endpoints (loopback) need `ADOPS_GUARD_TEST_ENDPOINTS=1`; config files other users can change
  are refused; an existing state directory must be private, and the journal and lock are never
  written through symlinks or hard links.
- A re-run after an ambiguous `--apply` settles it: `verified` when the value is in place.
- `--json` prints exactly one JSON document, also on errors.
- Google: the developer token is optional (Google sunset developer tokens on 9 September 2026); a
  failed OAuth token refresh with HTTP 5xx or 429 is retried as a temporary error.
- Google Ads (REST, API v25 by default): `accounts`, `status`, `report`, `budget set`, `pause`,
  `enable`, `channel-controls` (Demand Gen, leaf field masks), `audit` (click-quality heuristic),
  `spend-watch` (larger of billing counter and real-time metrics; refuses to count all-time spend;
  pauses first, each action on its own), `conversion-action list|create`.
- Meta Marketing API (Graph API v26.0 by default): `whoami`, `status`, `report`, `usage`,
  `set status`, `set budget`, `spend-cap`, `video-check`, and a usage guard that stops at 85 %.
- `snippets/landing-consent-conversion.html`: Consent Mode v2, an App Store click conversion and
  App Store Connect campaign links from `?ct=`.
- `adops-guard doctor [google|meta] [--app ID]`: read-only checks of the whole setup in the order
  you set it up (config file, state directory, credentials and their file modes, OAuth, account
  access, API access level, account budget headroom, auto-apply, conversion goals, Meta token and
  permissions, app, ad account status, prepaid or card, spending-limit headroom, rate tier,
  Instagram identity, advertisable apps), each failure with its fix. ✓/✗ marks in a terminal,
  plain ASCII in logs, one JSON document with `--json`; exit 1 when a check fails. Never prints a
  secret; flags a Meta token cut at 128 characters by the macOS Keychain prompt; tells a network or
  platform-side problem (HTTP 5xx) from a setup problem; prints where the docs it names are online.
  Platforms that are not set up yet are skipped, so the untouched example config passes.
- One-line fixes for known API errors (`adops_guard.hints`): Meta codes and subcodes (expired or
  invalid tokens, permissions, the development access tier, Development mode, identity checks, App
  Promotion, WhatsApp, `explore_home`, budget sharing, rate limits), Google error codes (Test access
  on a production account, quota, manager accounts, a disabled API, missing scope), OAuth refusals,
  credential-source and config-file errors, printed as `error: hint: ...`.
- Docs: setup from zero (`docs/setup.md`), troubleshooting (`docs/troubleshooting.md`),
  click-quality audit, Google Ads gotchas, Meta gotchas.
- Examples for cron, a systemd user service and a launchd agent.
- Offline test suite with fake Google Ads and Graph APIs; CI on Python 3.10 to 3.14, gitleaks, CodeQL,
  with actions pinned to commit SHAs.

[Unreleased]: https://github.com/deegitech/adops-guard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/deegitech/adops-guard/releases/tag/v0.1.0
