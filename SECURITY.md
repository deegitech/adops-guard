# Security policy

adops-guard handles advertising credentials and can change how much money an ad account spends, so we
take reports seriously.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

## Reporting a vulnerability

**Please do not open a public issue.** Report privately through GitHub:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability** (GitHub private vulnerability reporting).
3. Describe the problem, the affected version and command, and how to reproduce it.

Direct link: <https://github.com/deegitech/adops-guard/security/advisories/new>

If that form is not available, open an issue with the **Private contact request** template, with no
details at all, and we will open a private channel with you.

**Never include real tokens, refresh tokens, client secrets, account or customer IDs, or campaign
data** in a report. Use placeholders such as `1234567890` or `act_123456789012345`. If a real
credential was exposed while you found the problem, revoke it first.

## What to expect

- We aim to acknowledge a report within 7 days and to agree on a fix and a disclosure date with you.
- We will credit you in the advisory and the changelog, unless you prefer not to be named.
- This is a small team: timelines are goals, not guarantees.

## Scope

In scope, for example:

- a credential reaching output, an error message, the journal, the state file, a URL or argv;
- a way to send credentials to a host other than the official API hosts (for example through a
  config file in the directory you run the tool from);
- a dry run that changes anything on the platform;
- a way to bypass a spend ceiling, the account scope, the dry-run default or the read-back check;
- unsafe handling of the config file, credential files or the state directory.

Out of scope: bugs or abuse in the Google Ads or Meta platforms themselves (report those to Google or
Meta), and problems that need an already compromised machine or account.

## Good faith

We will not pursue anyone who researches and reports in good faith, avoids privacy violations and
service disruption, and only uses accounts they own or are allowed to test with.
