# Alpha A adapter: brief for the algo-trading session

Master PRD section 1.3 puts strategy code in the strategy's own repository and
asks for an adapter that exposes it to this platform; sections 50, 51 and 66
ask for that adapter and for decision parity with the strategy as it runs
today. The adapter belongs to `algo-trading`, which has its own session,
conventions and release process, so this platform does not write it. This
brief is what that session needs to build it, and what this platform does with
it once it is installed.

Everything below was checked against this branch (`feature/research-backtest`)
and against `algo-trading` as read on 2026-10-11. Where `algo-trading` is
described, it is as read, not as authority: that repository's own docs win.

## The contract, in one screen

The full contract is in
[`57-research-backtest`, Writing a strategy](../57-research-backtest/README.md#writing-a-strategy).
In short, an installed package registers a no-argument factory under the
`openalgo.strategies` entry point group; the object it returns has:

| Member | Required | What the platform does with it |
| --- | --- | --- |
| `interval` | yes | One of `1m 5m 15m 30m 1h D`. Bars of this size reach `decide`. |
| `decide(ctx)` | yes | Called at each bar's close inside the entry window. Returns signed weights per symbol (sum of absolute values at most 1; omitted symbols are 0), `{}` for flat, or `None` to hold. Orders decided at a close fill at the next bar's open. |
| `artifact_hash` | when it carries a model | Part of the version: a changed artifact is a new version. |
| `model_hash`, `adapter_version` | optional | Part of the version when declared. |
| `metadata` | optional | Recorded on the version, outside its hash. `training_start` and `training_end` (YYYY-MM-DD) here are the version's training period; backtests overlapping it are marked in-sample. |
| `warmup_days` | optional | Days of history a session loads before its first decision (default 5). |
| `initialize(setup)`, `on_fill(fill)`, `on_session_end()` | optional | Called at the same points in a backtest and a session. Load the model in `initialize`, not at import. |

`ctx` gives `time`, `day`, `minute` (IST), `symbols`, `bars(symbol)` (read-only
arrays up to this bar), `position(symbol)`, `equity`, `config` (read-only),
`rng` (seeded per instance) and `record(name, {symbol: value})`.

Strategy code cannot open a network connection, a database, a process or a
thread, and cannot import the order path; trying any of them is refused and
the run says which strategy tried. Reading files is allowed, so a frozen
artifact shipped as package data loads in `initialize`.

## Alpha A, mapped

As read from `algo_trading/paper/` (`paper_session.py`, `signal_service.py`,
`model_artifact.py`, `artifacts/alpha_a_deployment.json`):

| Alpha A today | In the adapter |
| --- | --- |
| Decides on the 09:15 bar's close, fills at the 09:16 open | `interval = "1m"`, configuration `entry_time = "09:15"`. The engine fills a close's decision at the next open, so the timing carries over unchanged. |
| One position a day: the rank-1 symbol, long or short by its side | `decide` returns `{pick: 1.0}` or `{pick: -1.0}` at the decision bar and `None` after it. Sizing is the platform's: `capital x allocation x leverage` from the configuration. |
| Exit at 15:10 (`paper_execution.DEFAULT_EXIT_TIME`, kept clear of the sandbox's 15:15 square-off) | Configuration `exit_time = "15:10"`, product MIS. The platform flattens it; the strategy does nothing. |
| Frozen `win_probability` model: `w`, `mu`, `sd` over 53 named features | `initialize` loads `alpha_a_deployment.json` from package data and checks it the way `ModelArtifact` does today. |
| `train_period: 2015-02-02..2024-01-01` | `metadata = {"training_start": "2015-02-02", "training_end": "2024-01-01", ...}`. Every research backtest over those dates is then marked in-sample. |
| `model_version`, `feature_version`, `code_version` | `model_hash = model_version`; `feature_version` and `code_version` in `metadata`; `artifact_hash` = SHA-256 of the artifact file. |
| An audit row per decision with every score (`paper_signals.jsonl`) | `ctx.record("win_probability", scores)` at each decision. The run keeps it as a hashed `records` parquet artifact beside its trades. |
| The universe snapshot is recorded with each decision | The configuration's `universe` is the snapshot, and each configuration is an immutable, hashed version. |

## What has to be true

1. **Features from `ctx.bars` only.** The model must see in a backtest exactly
   what it sees live, and `ctx.bars` is the only input that is the same in
   both. As read, Alpha A's 53 columns are built from bars alone by
   `paper_panel`, causally, from frames truncated to the decision bar, so
   `ctx.bars` can feed them. Reuse that live builder rather than the offline
   one, so a research backtest and the paper runtime compute the same
   columns; its two causality notes (`minutes_to_squareoff` is the scheduled
   constant, and a full session is assumed at 09:16) then hold in both. Ten
   columns are a cross-section of the whole universe at the decision bar, and
   the builder fails closed when a symbol is missing: the adapter should
   return `{}` that day rather than rank a partial universe. RES-1, a separate
   candidate, also reads the results calendar
   (`algo_trading/paper/results_calendar.py`), which is not a bar: an adapter
   for it would ship the calendar snapshot as versioned package data.
2. **History.** If the 53 features look back more than five days, set
   `warmup_days` so a session loads enough. In a backtest the strategy sees
   bars from the scenario's start, so it should return `None` until its
   features are defined rather than score on a partial window.
3. **Determinism.** The same bars must give the same scores and pick. Use
   `ctx.rng` for anything random; nothing else is seeded the same way in
   every environment.
4. **Decision parity (PRD sections 51 and 66).** Run the adapter in a research
   backtest over days the paper runtime has already decided, and compare day
   by day with `paper_signals.jsonl`: universe, every score to a stated
   tolerance, the pick and its side. Days that differ are listed with the
   reason, never averaged away. This is the test that makes the adapter
   Alpha A rather than something like it.

## Installing it here

1. Production runs the upstream OpenAlgo image, which has no Research
   module. Nothing below works until this fork's image is built and
   algo-trading's `OPENALGO_IMAGE_PIN` points at it (see this repository's
   `CLAUDE.local.md`).
2. Install the package into OpenAlgo's venv, as `make deploy` already does for
   the paper runtime's wheel. The Research registry discovers the entry point
   on its next read; the strategy page shows the version with its hashes, its
   declared training period and Properties.
3. Backtest, then deploy in the Sandbox, then Shadow beside it, then Live
   behind the Live gate (`RESEARCH_LIVE_ENABLED`, the version allowed for live
   trading, a completed Sandbox session, a capital limit and the deployment's
   name typed to confirm).

## Two things that collide if nobody decides

- **One sandbox account.** A research Sandbox session and the hosted paper
  runtime both trade the same OpenAlgo sandbox account. Run both on the same
  day and each sees the other's positions: the paper runtime's reconciliation
  would report positions it did not open. Run one or the other per day, or
  give each its own symbols.
- **The sandbox MIS loss limit** (Sandbox settings, off by default). If it is
  turned on, the sandbox can square off MIS positions before Alpha A's 15:10
  exit, with an order that is neither Alpha A's nor the research session's.
  `algo-trading/integrations/openalgo/CLAUDE.md` documents the sandbox
  behaviours the paper runtime depends on; this is a new one to add there
  before anyone turns it on.
