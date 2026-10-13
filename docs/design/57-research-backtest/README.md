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
        ctx.record("score", {"RELIANCE": 0.82, "INFY": -0.14})  # optional, see below
        return {"RELIANCE": -1.0}   # signed weights; omitted symbols are 0; None = hold
```

`ctx.record(name, {symbol: value})` keeps a per-symbol panel with the run:
scores, predictions, features, anything a later reader needs to audit why a
decision was taken (PRD section 19). Each call is one row per symbol at that
bar. The run writes them to a `records` parquet artifact (`timestamp`,
`instance`, `name`, `symbol`, `value`) with its size and SHA-256, streamed in
chunks so a long backtest's panel never sits in memory. Recording changes
nothing about the decision or the run's identity; a run that records nothing
has no such artifact, and one that does not complete leaves no file. The
reference strategy records the first-bar move it ranks on.

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

### A strategy as one uploaded file

A strategy does not have to be a package. Upload strategy file on the Research
page takes one `.py` file that defines a class named `Strategy` with the
contract above; the file name is the key, so it is lowercase letters, digits
and underscores (`opening_range.py` becomes `opening_range`). Files live in
`strategies/research/` (`research.strategy.STRATEGY_FILES`), beside the Python
host's `strategies/scripts/` and on the same volume, gitignored, so an upgrade
never touches them. Discovery lists them after the built-ins and the installed
packages, and neither can be shadowed by an upload.

The app never imports one. A package is installed by the operator on the
server; an upload arrives from a browser, and a trader's file may load a model
or a heavy library at import, which inside the one app worker would stall
every request. So `services.research_service.identify_file` reads a file's
identity in `python -m research.identify`, a short process under the same
guard and limits as a backtest, with 60 seconds to answer. The upload is
written beside the strategy files under its final name, checked there, and
moved into place only if it builds a strategy, so a refused file leaves
nothing behind. On every registry read a file is matched to its recorded
version by hash, with no process at all; only a file changed on disk is read
again. Its version identity is its name and a hash of its bytes; replacing it
is a new version and the old one stays.

### Properties: the training period and other details

What is known about a version is recorded beside it, never on it
(`research_version_properties`), because a version is the exact code and
model and is never edited, while what is known about it can be added or
corrected. On the strategy page, Properties on a version holds:

- **The training period**: the data the model was fit on, as two dates. A
  strategy can declare its own in `metadata`, and a period set on the page
  replaces it:

  ```python
  metadata = {"training_start": "2019-01-01", "training_end": "2023-12-31"}
  ```

- **Anything else worth knowing**, as named text: the model, its features,
  its target, a note. Up to 30, each name up to 60 characters and each value
  up to 500.

Properties are descriptive only (PRD section 5): nothing in them changes how a
version runs, and they are not part of a run's identity, so recording one
never makes an identical rerun compute again. What they change is how a
result reads: a backtest whose dates overlap a strategy's training period is
marked **in-sample** on its run page, naming the overlap, since a model scored
on data it was fit on looks better than it will trade. Live, Shadow and
Sandbox sessions trade new data and are never marked. Each run keeps the
properties as they were when it ran, in its provenance, so a later correction
leaves the record of what was believed at the time.

## Research as part of Python Strategies

Research is an extension of the Python strategies hosted at /python, not a
second strategy system: each strategy keeps one script and one set of settings,
its own in Python Strategies, and gains three tabs beside Code, Logs and
Schedule (`frontend/src/pages/python-strategy/`): **Backtest**, **Sandbox
days** and **Results**. The Research page is retired: its registry of packages
and uploaded files, experiments, deployments, Live panel and upload are gone
from the screen and their addresses lead to /python. A run's and a session's
own pages stay, reached from a strategy's tabs, and a script's run links back
to its strategy. The endpoints behind the retired pages remain on the server,
unused by the UI. A package such as algo-trading is code a script imports; it
does not trade by itself. A script runs on its own schedule in
its own process and places its own orders, usually against the sandbox in
analyzer mode. Research never starts, stops or edits one.

`services/research_hosted_service.py` follows every script, with no step and
nothing to fill in: the first time it sees one it records a
`research_script_links` row and makes it a strategy whose source is `script`,
keyed by the script's id, so it has versions and runs like any other.

- **Versions.** Each version is the script file's hash. A copy of the file is
  kept under `db/research/scripts/<hash>/`, taken before it is hashed so the
  copy is exactly what the hash names, and that copy is what a backtest of the
  version runs.
- **Settings.** There are none of Research's own. A run uses the script's
  schedule (start, stop, weekdays) and exchange, and the sandbox's starting
  capital and intraday leverage (`ScriptSettings`), copied into a
  configuration version so a past run still says what it ran with after either
  changes.
- **Its orders.** A script's orders are recognised by the `strategy` name they
  carry, learned rather than configured: the host names each run's log by its
  start (`log/strategies/<id>_<YYYYMMDD_HHMMSS>_IST.log`) and its last write
  ends the window, and every name on a sandbox order inside a window is the
  script's, unless another script was running at the same moment, when it is
  neither's. Learned names are kept in `learned_tags`, so a day stays
  recognisable after its log is pruned.
- **Sandbox days.** A day the script traded becomes a `HOSTED` run built from
  the sandbox's trades under its names. The sandbox's own square-off orders
  carry `AUTO_SQUARE_OFF` and close the net position in a symbol, so one is
  given to the script only up to what it held, in the direction that closes
  it. A day is recorded once it is over for the script (any earlier day, or
  today after its stop time once it has stopped), when its tabs are opened and
  by a 15:45 IST weekday job, under one lock so never twice. Positions are
  marked on Historify's 1m bars for that day when they are stored, otherwise
  at the script's own fill prices.
- **Backtests.** Asked for with dates only, and replayed unchanged; see below.

Both a recorded day and a replay end in fills Research did not decide.
`research.fills.book_from_fills` drives the engine's own book with them,
charging the scenario's cost model (intraday, by exchange), so costs, round
trips, positions and equity are computed exactly as for a strategy backtest and
every run page reads them the same way.

### Which symbols a script reads

No setting lists them: a script decides its own. A backtest starts from what is
known (the symbols of its own Sandbox trades and of its last backtest, fetched
first through Historify if missing). The simulator records each symbol the
script asks for and does not have (`asked_for`); the worker
(`replay_script` in `research/worker.py`) loads those from Historify and starts
the replay over from the first day, up to three rounds, so every day sees every
symbol. A symbol Historify does not hold sends the run back to the queue with
`missing_symbols` in its provenance; the app downloads them through Historify
with the requester's API key, once, and the run starts again, then names any
still missing. The bars served are frozen as a dataset for the record. A
script run carries no run hash: it reads Historify fresh each time and is a
record, like a session, never a reproduction to match or skip.

### Replaying a script

`research/script_replay.py` runs each trading day of the snapshot as a fresh
process, `python -m research.script_session`, started at the configuration's
entry time and ended at its exit time, which is how the strategy host runs a
script: module state starts clean each morning, and the day ends by ending
the process. Cash, positions and fills carry between days in one state file,
written when a day closes.

Inside a day:

- **Clock.** `time.time`, `time.sleep`, the `localtime` family and
  `datetime.now`/`date.today` read a virtual clock that starts at the entry
  time. `sleep` returns at once and moves it on; reaching the exit time runs
  the market to the close (fills, the square-off, open orders expiring) and
  ends the process. `time.monotonic` stays real, because `queue` and
  `threading` time their waits with it and a frozen monotonic clock would make
  every timed wait endless. pandas and pyarrow are imported before the clock
  is installed: their compiled datetime code hangs if it first meets the
  stand-in types, and a script importing them later gets the loaded copies.
- **API.** `research.script_sim` answers the OpenAlgo API on a loopback port of
  the process's own, and `HOST_SERVER` points there, so the `openalgo` SDK or
  any HTTP client reaches it unchanged. It answers from the scenario's frozen
  1m snapshot with only what had happened by the virtual time: the last price
  is the open of the minute bar forming then, history includes that bar flat
  at its open, longer intervals are built per day from its first bar. Orders
  fill as the sandbox fills them: market at the last price when placed, limit
  at once if marketable, otherwise on the first later bar that trades through
  it, stops when a later bar reaches the trigger. MIS squares off at the
  sandbox's default time per exchange and later MIS orders are refused; open
  orders expire at the close. An order that takes gross exposure past the
  configuration's buying power is refused for insufficient funds. An endpoint
  a backtest cannot answer is refused by name and counted on the run.
- **Files.** A write through Python's file functions to a path outside the
  day's folder goes to a copy of that path under `<folder>/outside/`, made
  from the live file first, and every later read or listing of the path sees
  the copy; a deletion is remembered, not performed (`research/overlay.py`).
  The hosted Alpha A, which keeps its audit under `ALGO_DATA_DIR`, replays
  unchanged and its live audit is never touched. Telegram and WhatsApp alerts
  are endpoints like any other and are refused and counted.
- **Walls.** The first thing the day's process does, while it has one thread,
  is restrict itself with Landlock (`research/landlock.py`): from then on the
  kernel lets it, and every thread it starts, write only inside its folder
  and connect over TCP only to its own port, whatever code asks, compiled
  libraries included. Then an audit hook that cannot be removed refuses any
  other connection, name lookup or datagram, any write that reached Python's
  lowest level unredirected, and any new process. On a kernel without
  Landlock only the hook stands, and the run records which it had.

A day the script fails or exits on is listed on the run with the time and the
reason, and the next day still runs, as the host would start it again. A day
still going after 300 seconds of real time is stopped and listed; it keeps
nothing, since state is written only when a day closes. Compiled code that
writes outside the folder (pyarrow, DuckDB) gets no copy: the kernel refuses
it. Known limits: a
script sleeping in several threads at once advances the one clock by every
sleep; waits that do not go through `time.sleep` take real time; pandas'
`Timestamp.now()` reads the real clock; the sandbox's MIS loss limit is not
modelled; and a recorded day is recorded on its own, so a position carried
overnight is not joined to its other day.

## Where a backtest's bars come from, and how far back

The first run of a scenario reads Historify once and freezes what it read
(`research/dataset.py`). When Historify does not hold a symbol over the
window, `services.research_service.enqueue_run` asks Historify to download it
from the broker first, through Historify's own job system and with the
requesting user's API key: 1m bars for every intraday interval, D for daily.
The run stays QUEUED with `research_runs.data_job` naming the job and its
progress in its message; the dispatcher never starts it while it waits, the
supervisor keeps polling while any run waits, and every write is a
compare-and-set on the run still waiting for that job. A finished download
releases it (the worker still names a symbol that has no bars); a failed or
cancelled one fails it, saying to log in to the broker or download in
Historify. A stored range that misses up to five days at either end covers
the window, so a weekend or holiday never triggers a download.

Every scenario loads history before its window: 45 calendar days of bars for
intraday intervals, 400 for daily, recorded as `execution_model.trade_from`,
the first traded day. The engine (`Engine(trade_from=...)`) and a script
replay start there: strategies read the earlier bars through `ctx.bars` (a
script through the history API), but nothing before it is decided, filled or
counted, so the result covers exactly the window asked for. Alpha A and RES-1
read 21 sessions before they decide; without this, a 30-day backtest of either
would have traded its last week only.

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
