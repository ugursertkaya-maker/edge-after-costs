# Contributing to EdgeAfterCosts

Thanks for considering a contribution. This document covers the things that are
specific to this project; the usual open-source etiquette applies otherwise.

## The one non-negotiable rule

**Nothing may add an order-execution path.**

EdgeAfterCosts is paper-only by construction, and that property is enforced by
tests rather than promised in prose (`api/app/safety.py`,
`api/tests/test_paper_only.py`, `api/tests/test_safety.py`). A change that
introduces any of the following will fail CI and will not be merged:

- a request-signing helper or HMAC in the trading path
- an API-key header or any stored exchange credential
- a call to an authenticated exchange endpoint
- an order-submission function of any kind

If you want to build live execution, fork the project and take ownership of that
decision there. Please do not open a pull request asking this repository to
relax the guard.

## Development setup

```bash
cp .env.example .env          # set POSTGRES_PASSWORD, SECRET_KEY, BOOTSTRAP_ADMIN_PASSWORD
uv venv --python 3.12 .venv   # or: python3.12 -m venv .venv
.venv/bin/python -m pip install -r api/requirements-dev.txt
cd web && npm install
```

Run the whole stack with `docker compose up --build`.

## Before you open a pull request

```bash
cd api && ../.venv/bin/python -m pytest tests -q
cd web && npm test && npm run typecheck
```

Both must pass. The backend suite is offline and deterministic by design: no
network, no wall clock, no database. If your change needs one of those to pass,
that is a signal the change is in the wrong layer.

## Standards for quantitative changes

This project's value is that its numbers can be trusted. Changes that touch
measurement are held to a higher bar than changes that touch plumbing.

- **No look-ahead.** Features must be causal: the value at index `i` may use
  `bars[0..i]` and nothing later. Labels must be purged and embargoed. If you
  add a feature, add the test that asserts a truncated history produces the same
  value.
- **In-sample is not a result.** Any performance claim must be out-of-sample and
  walk-forward validated. The dashboard renders in-sample columns dimmed for
  this reason; please keep it that way.
- **Do not move a threshold to improve a result.** The promotion gates exist to
  refuse exactly that. If a gate is wrong, argue that it is wrong on its own
  terms, in its own pull request.
- **Report what you tried, including the failures.** Losing candidates stay in
  the results table. A search that only shows its winners is not evidence.
- **An unmeasured value is not zero.** This holds in the code, the API payloads
  and the UI. If something was not measured, say so; do not render a blank as a
  zero or a missing gate as a passed one.
- **Costs are not optional.** Any new decision path must be evaluated on net
  edge after fees, spread and slippage.

## Standards for UI changes

- The dashboard is English-only.
- Distinguish "measured and found nothing" from "not measured". Those license
  opposite conclusions and the panels are built around the difference.
- Every panel gets an error boundary; one bad field must not blank the screen.

## Code style

- Python: type hints on public functions, `Decimal` for money in any path where
  rounding matters, docstrings that explain *why* rather than restate the code.
- TypeScript: strict mode, no `any` in new code.
- Comments should carry the reasoning that is not obvious from the code. Several
  modules in this repository document the trap they exist to prevent; that
  convention is deliberate, please follow it.

## Commit messages

Write what changed and why, in the imperative. The existing history uses a
one-line summary that states the finding or the fix, which is a good model.

## Reporting a problem

- Bugs and feature requests: use the issue templates.
- Security issues: **do not** open a public issue. See [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
