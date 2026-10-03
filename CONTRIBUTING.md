# Contributing to adops-guard

Thanks for helping. Bug reports, fixes, docs and new gotchas are all welcome.

## Ground rules

1. **No real data, ever.** No tokens, refresh tokens, client secrets, account, customer, campaign or
   page IDs, user names, e-mail addresses, budgets or campaign names from a real account: not in code,
   tests, fixtures, docs, examples, issues or commit messages. Use placeholders such as `1234567890`,
   `act_123456789012345`, `11111111111` or `com.example.mygame`. CI runs a secret scanner on every
   push.
2. **Tests stay offline.** Tests use the local fake APIs in `tests/fakes.py` (a real HTTP server on
   127.0.0.1). Never call a live API from a test.
3. **Safety is the product.** A change must not weaken the dry-run default, the ceilings, the account
   scope, read-back, journaling or credential handling. If a feature needs that, open an issue first.
4. **Label platform behaviour honestly.** Write "observed (Month YYYY, API vNN)" for things you saw,
   and link Google's or Meta's documentation for documented rules.
5. **No runtime dependencies.** The package uses the Python standard library only. Development tools
   go in the `dev` extra.

## Development setup

```bash
git clone https://github.com/deegitech/adops-guard.git
cd adops-guard
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

python -m pytest                       # all tests (Node.js, if installed, also runs the snippet's JavaScript)
python -m unittest discover -s tests   # the same tests with the standard library runner
ruff check . && ruff format --check .
```

Python 3.10 is the oldest supported version: avoid newer syntax (CI runs 3.10 to 3.14).

The test harness points the clients at a local mock server, which the tool allows only when
`ADOPS_GUARD_TEST_ENDPOINTS=1` is set; `tests/fakes.py` sets it for you. Never set it outside tests.

## Adding a command

- Reads go through `GoogleAdsClient.search` or `MetaClient.get`/`get_fields`/`get_all`.
- Writes go through `adops_guard.changes.execute` (or `run_prepared` for Google): plan, validate,
  `--apply`, read back, journal. Spend-increasing writes must call `adops_guard.guards.check_ceiling`.
- Add tests for the dry run, `--apply` with read-back, the refusal paths, and a mismatch.
- Update `README.md` (commands table, examples) and `CHANGELOG.md`.

## Adding a known error

When you hit a platform error with a clear fix, add it to `src/adops_guard/hints.py` (Meta code and
subcode, or Google error code) as one plain-ASCII line, add a row to `docs/troubleshooting.md`, and
mark platform behaviour that is not documented as observed, with the month. A test checks that every
code in `hints.py` appears in the troubleshooting table. If `adops-guard doctor` could detect the
problem with a read-only call, add a check to `src/adops_guard/doctor.py` and a test in
`tests/test_doctor.py`.

## Pull requests

- Keep them focused; describe what changed and why.
- Make sure `ruff` and the tests pass.
- Fill in the checklist in the pull request template.

By contributing you agree that your contributions are licensed under the MIT License, and you agree
to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).
