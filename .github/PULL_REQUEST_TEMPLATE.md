## What and why

<!-- What does this change, and why is it needed? Link the issue if there is one. -->

## Checklist

- [ ] `ruff check .`, `ruff format --check .` and `python -m pytest` pass locally.
- [ ] New behaviour has offline tests (fake APIs in `tests/fakes.py`), including the dry run and the refusal paths.
- [ ] Writes go through the plan / validate / `--apply` / read-back / journal path, and spend increases through `check_ceiling`.
- [ ] No real tokens, IDs, names, budgets or campaign data anywhere (code, tests, docs, commit messages).
- [ ] Platform behaviour is labelled "observed (Month YYYY)" or linked to the official documentation.
- [ ] `README.md` and `CHANGELOG.md` are updated if users will notice the change.
