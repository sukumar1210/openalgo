# Research Backtests (`/research`)

Research is the strategy research layer of this fork: a registry of immutable
strategy versions and configuration versions, experiments made of scenarios,
and a deterministic backtest engine that runs strategies alone, side by side
with isolated capital, or together in one shared portfolio. Every result keeps
the exact inputs that produced it.

The product requirements are in [`docs/prd/research-backtest.md`](../../prd/research-backtest.md).

## Baseline

Recorded before any product change, on branch `feature/research-backtest` at
`782adb740` (forked from `dev` on 2026-10-06), on the development server at
`/srv/sources/openalgo-dev`. These are the numbers a later run is compared
against: a change is judged by what it moves, not by whether the suite is green,
because on this machine it is not green before the change.

| Suite | Command | Result |
| --- | --- | --- |
| Backend | `uv run pytest test/ -q --continue-on-collection-errors` | 7262 passed, 36 failed, 81 skipped, 6 xfailed, 7 errors (15m20s) |
| Ruff | `uv run ruff check .` | 1036 findings already present |
| Biome | `npx biome check ./src` (read-only; `npm run check` writes) | 114 errors, 4 warnings already present |
| Vitest | `npx vitest run` | 3424 passed, 29 failed in 8 files |
| Frontend build | `npx tsc -b && npx vite build` | passes |

What the existing failures are, so nobody chases them as regressions:

- **Four collection errors** (`test_openscript_engine_reads.py`,
  `test_openscript_live_bars.py`, `test_openscript_runner_facts.py`,
  `test_strategy_residual_safety.py`). They import siblings as `test.<module>`,
  and this machine's Python 3.13 ships the standard library's own `test`
  package, which is found first. CI runs Python 3.12 without it.
- **Environment.** The worktree has no `.env`, so anything that needs
  `API_KEY_PEPPER` or a live broker module fails at import (`test_mstock.py`,
  the Telegram tests, `test_rate_limits_mock.py`).
- **Timing under load.** The gthread deploy and memory tests, the
  rate-limiter tests and most of the vitest failures (`StrategyBuilder.test.tsx`
  took 200s) are timeouts: the backend and frontend suites ran concurrently on
  one machine.
- **npm.** `frontend/package.json` declares `"openalgo": "file:.."`, which
  needs a `package.json` at the repository root. None exists. npm 10 (CI's)
  tolerates it and npm 9.2 (Debian's) refuses it, so install with
  `npx -y npm@10 ci` on this machine. The dependency is unused.

The full lists are kept with the branch's first commit message rather than here,
because they describe this machine on this day and not the feature.
