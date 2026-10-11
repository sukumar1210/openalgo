# Research Platform: Completing the Master PRD

The plan and status for everything the master PRD
([`research-platform.md`](../../prd/research-platform.md)) asks for beyond what
[`57-research-backtest`](../57-research-backtest/README.md) (Phases 1 to 4) and
[`58-research-sandbox`](../58-research-sandbox/README.md) (Phase 8) built. The
PRD's own gate comes first: [`gate-review.md`](gate-review.md) reviews Phase 4
and names the six architectural changes the rest depends on.

## Order of work

The PRD numbers phases 5, 6, 7, 9. They are built in a slightly different order,
because each later one stands on the seams the earlier one adds:

| Step | What | PRD | Status |
| --- | --- | --- | --- |
| 1 | Gate review, this plan | 51 | Done |
| 2 | Strategy contract: lifecycle hooks, seed, model hash, adapter version | 1.2, 2, 3, 14 | Done |
| 3 | Full ledger: one journal for every environment, position snapshots, audit | 16-18, 43, 52 | Done |
| 4 | Portfolio analytics and the portfolio loss limit | 11, 15, 37, 53 | Done |
| 5 | Phase 2-4 gaps: dataset provider, experiment objective and status, registry and configuration screens | 6, 7, 32, 33 | Done |
| 6 | Execution adapters, Shadow and Live | 25, 30, 31, 56, 58 | Done |
| 7 | Production hardening | 44, 54 | Done |
| 8 | Alpha A adapter (in algo-trading, from [this brief](alpha-a-adapter.md)), final verification ([verification.md](verification.md)) | 1.3, 50, 51 | Brief and verification done; the adapter is algo-trading's |

Phase 7 follows Phase 9 here because most of its hardening (resuming a failed
session, alerts, limits) acts on the session the execution adapters reshape.

## Step 2: the strategy contract

Every hook is optional, so a strategy written for `decide` alone keeps working
and keeps its version hash.

- `metadata` may be a method or a mapping; either is recorded on the version.
- `initialize(ctx)` runs once before the first bar of a run or session, with the
  symbols, the configuration, the interval and the instance's seed.
- `on_fill(fill)` runs after every fill applied to that strategy's own book, with
  a read-only fill: symbol, signed quantity, price, fees, slippage, reason, time.
- `on_session_end()` runs after the last bar of each trading day, in a backtest
  and in a session alike, so a strategy sees the same lifecycle in both.
- Every hook runs as strategy code (no network, database, process or thread).
- **Seed.** Each instance gets one, derived from its strategy version, its
  configuration hash and its position, so the same inputs give the same seed in
  every environment, and `ctx.rng` is a NumPy generator seeded with it. The
  seeds are recorded in the run's provenance.
- **Model hash and adapter version** are read from the strategy object when it
  declares them (`model_hash`, `adapter_version`) and stored on its version. Each
  joins the version hash only when present, so no existing version changes
  identity.

## Step 3: the full ledger

- **One order identity everywhere.** The engine reserves the order's number at
  the decision in a backtest too, so `research:<run>:<seq>` names the same
  thing in every environment. Sequence numbers change, so `ENGINE_VERSION`
  becomes 2; old runs keep the version they recorded. Building it moved the
  order lifecycle (`close_bar`, `_intents_from_pending`, orders in flight) from
  the live engine into the shared one, which closed a parity gap the tests had
  never exercised: a backtest settled fully crossed intents at the next bar's
  open and a session at the decision close. Both use the close now
  (`test_crossed_intents_settle_at_the_decision_close_live_and_in_a_backtest`).
- **Signals are recorded when they change.** A strategy answering the same
  weights bar after bar is one decision, recorded once per day; the targets it
  leads to carry the id of the signal behind them, and each order the ids of
  its targets, so a fill traces back to the answer that caused it.
- **Unrealized P&L** is snapshotted at the close of each trading day in a
  backtest (an intraday book is flat by then, and its equity curve is per bar
  already) and at each bar's mark in a session, where it is what an operator
  watches.
- **Facts are persisted once.** `ledger_events` gains, in every environment:
  `SIGNAL` (a strategy's answer when it gave one), `TARGET_POSITION` (each
  target that changed, with its reason), `ORDER_INTENT`, `SQUARE_OFF` (forced
  exits with their cause) and `SESSION_EVENT`. A new `ledger_positions` table
  snapshots every book's positions, average price, mark and unrealized P&L at
  the close of each trading day.
- **Derived events are computed, never copied.** `FILL`, `FEE`, `SLIPPAGE`,
  `POSITION_UPDATE` and `REALIZED_PNL` already live in `ledger_fills` and
  `ledger_trades`; the journal derives them. Copying them would put the same
  money in two places that could disagree.
- **The journal** (`/research/api/runs/<id>/ledger`) merges both into one ordered
  stream carrying the PRD's field set (section 16): strategy, version,
  configuration, experiment, scenario, session, instance, signal, platform and
  execution order ids, fill and trade ids, symbol, side, quantity, requested and
  fill price, fees, slippage, P&L, time.
- **Audit tools.** A reconciliation report per run (trade P&L plus costs equals
  equity change; contributions sum to the portfolio; positions from fills equal
  the snapshots; every platform order matches its venue's record), and a Ledger
  tab on the run page with filters and a CSV download.

## Step 4: portfolio analytics

- The engine records gross and net exposure per bar for every book and
  portfolio, beside equity.
- New measures: average and peak exposure, capital utilisation (exposure over
  buying power), position overlap between strategies (same side and opposite
  side), netting impact (notional and costs saved by crossing inside the
  portfolio), conflict rate, capital competition (reductions by the exposure
  limit), and the correlation of strategies' daily returns.
- Charts: drawdown and exposure beside the equity curve. Comparison: scenarios
  side by side, isolated against shared for the same strategies, and runs
  grouped by configuration version.
- **Portfolio loss limit.** A scenario's risk policy may set a daily loss limit
  and a daily profit target per portfolio, judged at each bar's close by
  `services.risk.evaluate_aggregate`. A breach squares the portfolio off and
  takes no new entries for the rest of the day (the PRD's risk rejection).

## Step 5: Phase 2-4 gaps

- `DatasetProvider` with `HistorifyDuckDBProvider` behind it; datasets record
  their source, symbol count and first and last bar, backfilled from the stored
  snapshots rather than from a default.
- Experiments gain an objective; their status is derived from their runs.
- Results report final capital.
- The configuration history shows each version's hash.
- The registry page shows each strategy's status, last run and its P&L, with
  actions: Backtest, deploy to Sandbox, Shadow or Live, Pause and Stop.

## Step 6: Shadow and Live

The session gets the seam the PRD draws (section 25): an execution adapter.

| Environment | Adapter | Where orders go |
| --- | --- | --- |
| `SANDBOX` | Sandbox door (exists) | `/api/v1/researchsandbox/*`, the sandbox's `OrderManager` |
| `SHADOW` | Simulation inside the worker | nowhere: filled at the latest tick with the scenario's modelled slippage and costs |
| `LIVE` | Live door | `/api/v1/researchlive/*`, through `services.strategy_module.order_dispatch` (fresh broker authorisation per order, `force_live=True`, Action Center bypassed) |

The session, engine, books and ledger are the same for all three. Each door
refuses a run of any other environment, so a Sandbox or Shadow session cannot
reach the live door even by mistake.

**Live needs, all of them, every time:**

1. The server allows it: `RESEARCH_LIVE_ENABLED = 'True'` in `.env`, off by
   default (the `MCP_HTTP_ENABLED` pattern), so a fresh install cannot trade
   live from Research at all.
2. Every strategy version in the scenario was enabled for live by the operator
   on its strategy page.
3. The scenario has completed at least one Sandbox session (promotion).
4. A capital limit on the deployment that the scenario fits within.
5. A typed confirmation when deploying.
6. A broker session at the moment each order is placed.
7. The kill switch is off.

Every live order is also checked at the door against the deployment's buying
power. The platform-to-broker order mapping is written by the door before the
order is placed and completed after, so a restart finds what was sent. Partial
fills are applied as filled; the remainder is cancelled. **Broker
reconciliation** compares every platform order with the broker's own record,
and the platform's expected positions with the broker's position book, and
reports differences (other trading on the same account makes a difference a
warning, not a halt). **The kill switch** stops every live session, closes its
positions and refuses new live sessions until it is released.

**Shadow** runs beside a Sandbox or Live deployment of the same scenario and is
compared with it: orders, fills and P&L, bar by bar.

### As built

- The session takes its adapter in `load()`: `AppClient` with the Sandbox
  door, `ShadowExecution` (in the worker; fills at the latest tick, and the
  engine charges the scenario's slippage as a backtest does), or `AppClient`
  with the Live door (`services/research_live_service.py`, namespace
  `/api/v1/researchlive`). Each door refuses a session of any other
  environment; a Shadow session calls none.
- An order is "adding" unless it is on the other side of the session's filled
  position and no larger than it. A flip counts as adding. Only adding orders
  face the kill switch, the approvals and the capital limit; an exit is always
  sent.
- The Live door's order record is written PLACING before the broker call. A
  row still PLACING two minutes later was cut off by a crash; its status is
  UNKNOWN, and the session stops trading that symbol, closes everything else
  and ends FAILED saying what to check, instead of guessing either way.
- Fills arrive as the strategy module's do: one critical-lane subscription to
  `order.update` (`research_live_service.start()`, called at boot before any
  session resumes), matched by broker order id through an index. A session's
  status read is a row read. Only an order quiet for five seconds is checked
  with the broker (`fetch_order_status`), at most once per order every five
  seconds, and straight after a cancel. Each write is a compare-and-set on the
  status and filled quantity it read, so a stale "open" cannot land on the fill
  that came after it when two update callbacks run at once under gthread. An
  update that lands before the broker answers the placement has no row to
  match yet and waits for that five-second check (the strategy module replays
  such updates instead; not needed at this cadence).
- A cancelled order that partly filled is read from the tradebook and applied
  as filled (`LiveEngine.apply_partial_fill`: crossed books keep their share,
  carriers split what filled by largest remainder). A tradebook that cannot be
  read leaves the order open and checked again, never "nothing filled".
- Building it found a gap in every environment: a stopping session closed
  only what it held when the stop began, so an entry still in flight that
  filled afterwards stayed open. A stopping session now squares off again
  every ten seconds; what an order in flight is already closing is never
  ordered twice.
- Live approval is its own table (`research_live_approvals`), because a
  version is immutable and permission is a decision about it. The kill switch
  is a `research_controls` row each Live session reads on every pass.
- The session page shows its environment, whether its venue is answering, and
  links to the same scenario's session that day in another environment;
  `/research/compare/<a>/<b>` sets two runs side by side. The Research page has
  a Live panel: whether the server allows Live, the capital live deployments
  may hold together, the kill switch, today's live sessions, and a check against the broker's own orders
  and intraday positions.
- Every Live door test replaces the broker with a recording fake; one test
  runs OpenAlgo's real live pipe up to the placement call and reads that it
  asked for `force_live`.

## Step 7: hardening

- Limits: CPU time per backtest (`RESEARCH_MAX_CPU_SECONDS`), a wall-clock job
  timeout (`RESEARCH_JOB_TIMEOUT_SECONDS`), workers at lower scheduling
  priority, and a cap on concurrent sessions (`RESEARCH_MAX_SESSIONS`).
- An unexpected worker error resumes the session through the supervisor (the
  "resume, else square off" policy) instead of ending it with positions open.
- Every log line carries run, experiment, scenario and, where there is one, the
  instance and platform order id.
- Telegram alerts, sent by the app (workers cannot reach Telegram): session
  started, failed, completed with its P&L, a strategy failure, stale data, a
  live order refused, a reconciliation difference, the kill switch.
- An operations panel: workers running, their memory, last heartbeat and
  lease; stop all sessions.
- Pruning of run logs and artifacts older than a set age.
- A backtest worker refuses network connections for its whole life, not only
  while strategy code runs: it never needs one.

### As built

- **Resume, else square off.** An error that escapes a session's loop never
  ends a session that may hold positions: the worker exits, and the supervisor
  restarts it to resume from the ledger (`RESEARCH_RESTARTED=resume`). The
  last of `SANDBOX_RESTARTS` resumes only to close what is held (`close`). If
  that worker stops too, the session ends FAILED naming where its positions
  may still be open. A session that never sent an order ends FAILED at once
  with the reason. Building this found the session ended FAILED in place on
  any unexpected error, with nothing restarting it.
- **Closing messages are written from the end state.** A stop that timed out
  with positions held could say it had closed them; the message is now the
  cause followed by what was actually closed or left open.
- **Limits after exec.** Workers start through `_LIMITS_BOOTSTRAP`
  (`python -c`), which sets the memory and file limits in the child and runs
  the worker module as `__main__`, the strategy host's pattern, because
  `preexec_fn` is unsafe in a threaded parent. Backtests add a processor-time
  limit (`RESEARCH_MAX_CPU_SECONDS`, 3600), a wall-clock limit enforced by the
  supervisor (`RESEARCH_JOB_TIMEOUT_SECONDS`, 7200) and nice 10; sessions get
  none of these, since stopping one would orphan its positions. Sessions are
  capped at `RESEARCH_MAX_SESSIONS` (4). Each stop says which limit it hit.
- **Logs** carry `session=`/`run=`, `experiment=`, `scenario=` and `order=` as
  key=value. The platform order id is per session and decision; a decision
  nets every book, so a line about an order names the session, not one
  instance.
- **Alerts** go to Telegram from the app the way Flow sends them (bot on, user
  linked, notifications on, the shared alert pool): a session opened or not
  opened (with why), a session ended (net P&L, or why it failed), a live order
  refused, the kill switch. Stale data that pauses a session is shown on the
  session page but not alerted: the app sees no event for it.
- **Operations panel** on the Research page, and stop all sessions.
- **Pruning** covers run logs only (`RESEARCH_LOG_RETENTION_DAYS`, 30). A run's
  artifacts are its result and are kept; deleting them would quietly empty
  the history a later comparison reads.
- **No network in a backtest worker**, by a second audit hook
  (`research.strategy.refuse_network`): no connection, datagram or name lookup
  from anything in the process. The strategy hook now also covers lookups and
  datagrams, so a strategy that tries is named.

## Decisions taken without the author

Each can be reversed; say so and it changes.

- **Alpha A's adapter is handed to the algo-trading session**, with a brief,
  rather than written into that repository from here. The PRD puts it there
  (section 1.3), and that repository has its own session and conventions.
- **Live is off until `.env` turns it on**, with a yes/no switch like
  `MCP_HTTP_ENABLED`. Capital is bounded per deployment, where the operator
  types its limit and its name; there is no server-wide ceiling, as the
  strategy module has none.
- **The kill switch covers Live only.** Sandbox and Shadow sessions are
  stopped from the operations panel instead.
- **Shadow fills at the latest tick plus modelled slippage**, because a tick is
  all a Shadow session has; Sandbox fills at bid or ask from a quote.
- **No OS-level sandbox for strategy code.** Strategies are the operator's own
  code, the same trust the Python Strategy Host extends to scripts that place
  live orders by design. The in-process boundary stops accidents; code written
  to defeat it is outside the threat model of a single-user, self-hosted
  platform.
