# Research Backtests (`/research`)

Research is the strategy research layer of this fork: a registry of immutable
strategy versions and configuration versions, experiments made of scenarios,
and a deterministic backtest engine that runs strategies alone, side by side
with isolated capital, or together in one shared portfolio. Every result keeps
the exact inputs that produced it.

The product requirements are in [`docs/prd/research-backtest.md`](../../prd/research-backtest.md).
This release covers its Phases 1 to 4; the rest is listed under *Deferred*.
For a reviewer's walkthrough of the code path, before and after, see
[`research_feature_flow.md`](research_feature_flow.md).

## Non-negotiables

1. **A backtest cannot reach a broker.** The worker installs an import guard
   before anything else is imported that refuses the broker plugins, the
   sandbox, order placement, the broker history service and the auth-token
   store (`research/worker.py`). There is no setting that turns it off, and it
   covers strategy plugin code, which is imported after it.
   `test/research/test_research_runs.py` proves the whole backtest path imports
   none of them. While strategy code runs it also cannot open a network
   connection, a database, a process or a thread (an audit hook; see
   `58-research-sandbox` for why and for what it cannot stop), and the
   worker's environment carries no broker credentials or app keys.
2. **Inputs never change after they are used.** Strategy versions, config
   versions, scenarios, datasets and ledger rows refuse ORM updates
   (`database/research_db.py`). A changed strategy package is a new version row;
   an edited configuration is a new version row.
3. **A run says what produced it.** Every completed run carries a `run_hash`
   over its strategy versions, config hashes, dataset hash, mode, cost and
   execution model, risk policy, `ENGINE_VERSION` and `CONFLICT_POLICY`, plus a
   hash of the engine's own sources. At most one COMPLETED run may hold a given
   `run_hash` (a partial unique index), so a reproduction is never stored as a
   second, independent result.
4. **Risk rules come from `services/risk/`.** Stops and targets are evaluated by
   `services.risk.evaluate_position`; the engine translates, it does not decide.
5. **No CPU work in the app process.** Each run is its own subprocess, so a
   backtest cannot freeze the single eventlet worker, and one scenario's crash
   or memory blow-up cannot touch another's.

## How a run works

```
UI (/research) -> blueprints/research.py -> services/research_service.py
   enqueue: QUEUED row, then dispatch() spawns `python -m research.worker <run_id>`
   (cwd = repo root, output to log/research/run_<id>.log, address-space and
   descriptor limits, numeric libraries single-threaded)
worker: guard -> claim QUEUED->RUNNING -> resolve strategy versions -> dataset
   (first run reads Historify once and pins a parquet snapshot to the scenario;
   later runs read only the snapshot and verify its hash) -> run_hash
   (a completed twin ends the run as CANCELLED and points at it) -> Engine
   -> metrics -> ledger rows + equity parquet + COMPLETED, in one transaction
supervisor (shared executor "research", one thread): polls, reaps, dispatches
   the queue up to RESEARCH_MAX_CONCURRENT, settles runs whose process died
```

Every status write is a compare-and-set on the status the writer expects, so a
cancelled run cannot be completed by a worker that finishes late. At startup
`fail_orphaned_runs` marks anything left QUEUED or RUNNING as FAILED, because the
process that owned it is gone.

## The engine

`research/engine.py`. A *book* is one strategy instance's virtual account; a
*portfolio* is a group of books trading as one account. Single is one portfolio
with one book, isolated is one portfolio per book, shared is one portfolio with
every book. That grouping is the only difference between the modes.

- **Timing.** Orders decided at a bar's close fill at the next bar's open. An
  intraday position with no later bar that day fills at the close instead of
  surviving the night. MIS positions are flattened from the configured exit
  time; everything is flattened on the last bar of the data.
- **Shared netting (`CONFLICT_POLICY = net-v1`).** Every book is filled with its
  full intent at the same reference price, so each keeps the P&L of its own
  decision. The portfolio sends only the net order; costs are charged on that
  order and allocated to the books trading in its direction, pro rata, the last
  one taking the rounding remainder. A BUY 100 and a SELL 100 send nothing,
  cost nothing, and still leave a long and a short book.
- **Shared exposure limit.** When the netted portfolio would exceed
  `shared capital x exposure limit`, every book's new target is scaled by one
  factor. Reductions are counted per book.
- **Costs.** `portfolio/costs.py` presets. This change adds `india_intraday`
  (brokerage 0.03% capped at Rs 20, STT 0.025% on sells, stamp 0.003% on buys).
  Slippage is a separate basis-point charge on the net order.
- **Data.** Bars come from a `DatasetProvider` named by the scenario's
  `source`; the one provider is Historify's DuckDB store. A provider never
  fills a gap: a symbol with no bars is refused by name, and a missing bar is
  no price, not zero. Each dataset records its source, symbol count and first
  and last bar. A strategy is given its bars through `Context` only; nothing
  stops plugin code opening `historify.duckdb` itself (DuckDB opens files in C,
  outside the strategy-code hook), so that rule is the contract's, not the
  guard's.
- **Registry and experiments.** A strategy's row shows where it stands (ready,
  deployed, trading now, not installed) and its own P&L in the latest finished
  run or session, and it can be backtested, deployed, paused or stopped from
  there: the registry builds a one-strategy scenario in the strategy's own
  experiment and reuses it when the same action is repeated. Pause and stop
  act on whole sessions, never on one strategy inside a shared portfolio. An
  experiment states its objective; its status is derived from its scenarios'
  latest runs, never stored.
- **Orders and the ledger (engine version 2).** A decision becomes an order the
  moment it is made, with its number reserved, so `research:<run>:<seq>` names
  the same order in a backtest, the Sandbox or a broker. Intents that cross
  completely settle at the decision bar's close, as they do live. Beside orders,
  fills and trades the engine records each changed strategy answer (`SIGNAL`),
  each target (`TARGET_POSITION`, or `SQUARE_OFF` when forced), each order
  (`ORDER_INTENT`) and each position held at a day's close (`ledger_positions`),
  all on one sequence. The run page shows them as one journal with the PRD's
  fields, downloadable as CSV, and checks that the ledger reconciles
  (`services/research_ledger_service.py`; the design is in
  [`59-research-platform`](../59-research-platform/README.md)).
- **Portfolio limits.** A scenario may set a daily loss limit, a daily profit
  target and a lock-profit floor (rupees). At each bar's close the portfolio's
  P&L for the day is judged by `services.risk.evaluate_aggregate`; a breach
  closes everything the portfolio holds and takes no new entries until the next
  day. Isolated strategies are each their own portfolio.
- **Analytics.** Per bar, every book's gross exposure and every portfolio's
  gross and net exposure (of its netted positions). Per run: final capital,
  average and peak exposure, buying power used, time in the market, and each
  strategy's share of the result; with more than one strategy, orders avoided,
  value netted and costs saved by netting, how often orders crossed, how many
  targets the exposure limit trimmed, how often each pair held a symbol on the
  same or opposite sides, and the correlation of their daily returns. The run
  page charts drawdown (on every bar) and exposure; the experiment page sets
  isolated beside shared for the same strategies; a strategy's page shows how
  each configuration did.
- **Metrics.** Trade statistics from round trips; drawdown from the bar-level
  equity curve; CAGR, Sharpe and Sortino from `portfolio.analytics.summary`
  (openstatz). CAGR is withheld below a one-year window.

Known simplification: stops are detected on the close and exit at the next
open. Intrabar stop fills are not modelled.

## Writing a strategy

A strategy is an object with `interval` and `decide(ctx)`, built by a
no-argument factory, and registered by an installed package:

```toml
# pyproject.toml of the strategy package
[project.entry-points."openalgo.strategies"]
alpha_a = "algo_trading.research_adapter:AlphaA"
```

```python
class AlphaA:
    interval = "5m"
    artifact_hash = "<sha256 of the frozen model>"    # optional
    metadata = {"release": "...", "model_version": "..."}  # optional, recorded

    def decide(self, ctx):
        # ctx.time / ctx.day / ctx.minute (IST), ctx.symbols, ctx.bars(sym)
        # (read-only arrays up to this bar), ctx.position(sym), ctx.equity,
        # ctx.config (read-only).
        return {"RELIANCE": -1.0}   # signed weights; omitted symbols are 0; None = hold
```

The sum of absolute weights may not exceed 1. A weight of 1 is the instance's
`capital x allocation x leverage`. The strategy cannot see a broker, the future
or another strategy's book, in every mode. Its version is the distribution name
and version, a hash of the package's Python sources, and `artifact_hash`, plus
`model_hash` and `adapter_version` when the strategy declares them.

Optional hooks, called only when defined, at the same points in a backtest and
in a session (`research/strategy.py`):

```python
    metadata = {...}                  # or def metadata(self): return {...}

    def initialize(self, setup):      # once, before the first bar
        # setup.symbols, setup.interval, setup.config, setup.seed, setup.rng
        self.model = load_frozen_model()   # heavy work belongs here, not at import

    def on_fill(self, fill):          # every fill on this strategy's own book
        # fill.symbol, fill.qty (signed), fill.price, fill.fees, fill.slippage,
        # fill.reason ("signal", "stop", "target", "exit_time", "eod", ...), fill.time

    def on_session_end(self):         # after the last bar of each trading day
        ...
```

Import and the factory run inside the app process whenever the registry is
read, so both must be cheap; load models and artifacts in `initialize`. Every
hook runs as strategy code: no network, database, process or thread. Each
instance has a seed derived from its version, configuration and position, the
same in every environment and recorded in the run's provenance; draw random
numbers from `ctx.rng` (or `setup.rng`), a generator seeded with it.

## Decisions taken against the PRD

- **One database, `research.db`, not research plus ledger.** A run's ledger and
  its COMPLETED status are committed together; across two files a crash between
  the commits would leave a completed run with no ledger. Tables are prefixed
  `research_` and `ledger_` so a split later is mechanical.
- **UI endpoints are session-authenticated under `/research/api`, not
  `/api/v1`.** Backtests are operator actions from the browser; keeping them off
  the API-key surface keeps them off webhooks and every external caller.
- **A rerun of identical inputs returns the existing result** rather than
  computing it again (PRD section 17). Determinism itself is pinned by
  `test_identical_inputs_give_identical_ledgers`.
- **Capital lives on the configuration**, so a scenario like "A Rs 3L, B Rs 2L"
  uses config versions with those capitals; every scenario therefore has an
  explicit configuration record (PRD section 31). Shared scenarios may also set
  a shared capital and an exposure limit.
- **Isolated books have no portfolio limit**: by definition they share no
  portfolio constraints (PRD section 8).

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `RESEARCH_DATABASE_URL` | `sqlite:///db/research.db` | The research database |
| `RESEARCH_DATA_DIR` | `db/research` | Dataset snapshots and run artifacts |
| `RESEARCH_MAX_CONCURRENT` | `2` | Runs executing at once |
| `RESEARCH_MAX_MEMORY_MB` | `2048` | Address-space limit per run |

## Deferred

- **Phase 5:** position snapshots and a cash journal in the ledger; aggregate
  limits through `services.risk.evaluate_aggregate`; daily-loss limits.
- **Phase 6:** a matrix experiment planner that generates explicit scenarios.
- **Phase 7:** comparison extras (return correlation, position overlap, capital
  utilisation, conflict rate as a share), a drawdown chart.
- **Phase 8:** built as the Research Sandbox; see
  [`58-research-sandbox`](../58-research-sandbox/README.md). Shadow mode and
  OS-level isolation of plugin code remain open.
- **Phase 9:** built; Shadow and Live are in
  [`59-research-platform`](../59-research-platform/README.md). Alpha A parity
  remains the open gate before trading Alpha A live.
- **The Alpha A adapter**, written in algo-trading as an `openalgo.strategies`
  entry point.
- **Pruning run logs and artifacts.** Both are kept indefinitely today.
- **Diagrams.** docs.openalgo.in is upstream's site and this is a fork-only
  feature, so its diagrams are not updated.

## Deployment

Production on this machine runs the upstream image, so none of this reaches the
running instance until a fork image is built from `main` and algo-trading's
`OPENALGO_IMAGE_PIN` points at it. A strategy package must also be installed in
that image's interpreter to appear in the registry.

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
