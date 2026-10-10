# Research Sandbox (`/research`, Phase 8)

A scenario that has been backtested can be deployed to the **Sandbox**: each
scheduled trading day it trades live market data in its own session, with every
order filled by OpenAlgo's sandbox and never by a broker. The strategies, their
versions and configurations, the isolated and shared modes, the books, the
ledger and the attribution are the backtest's own, so a strategy keeps its
identity, portfolio semantics and accounting when the execution environment
changes underneath it (PRD section 42).

Requirements: [`docs/prd/research-sandbox.md`](../../prd/research-sandbox.md).
The backtest side this builds on: [`57-research-backtest`](../57-research-backtest/README.md).

## Non-negotiables

1. **A session cannot reach a broker, by construction, twice.**
   - The worker process installs the backtest's import guard before anything
     else, minus only the shared HTTP client. It cannot import the broker
     plugins, order placement, the sandbox, `restx_api`, `blueprints` or the
     auth-token store. `test_research_sandbox_deploy.py` proves it in a fresh
     process, including that a strategy reaching for order placement is refused.
   - Strategy code cannot reach OpenAlgo's own order API either. The worker
     holds an API key, and `/api/v1/placeorder` honours that key and follows
     the analyzer switch; with only the import guard, a strategy using the
     `openalgo` SDK sent an order there (reproduced, and pinned by
     `test_strategy_code_cannot_reach_the_order_api_over_http`). So the key
     leaves the environment before any strategy is imported, and while
     strategy code runs (`research.strategy.strategy_code`: plugin import,
     setup, every `decide`) an audit hook refuses any network connection,
     database, process or thread. The same hook guards backtests.
   - The only order door the worker itself calls, `/api/v1/researchsandbox/*`,
     calls the sandbox's `OrderManager` directly and never
     `place_order_with_auth` or the analyzer switch. Switching the platform to
     live cannot route a session's order to a broker (UAT PAPER-006,
     `test_research_sandbox_exec.py`).
2. **The sandbox is not the source of truth for strategy capital.** The account
   is one Rs 1 crore account shared with everything else (algo-trading's paper
   runs included) and merges positions per symbol. Each strategy's book in the
   engine is what the session reports (PRD section 8).
3. **One worker per session.** A lease on the run row, renewed with every
   heartbeat; a worker that loses it stops at once.
4. **Nothing is assumed filled.** An order is tracked until the sandbox reports
   a final state, and applied at the sandbox's own price.

## Shape

```
research_deployments  (scenario + schedule, ACTIVE | STOPPED)
   |  scheduler: start/stop cron in IST on chosen days, exchange calendar check
   v
research_runs (environment = SANDBOX, one per deployment per trading day)
   |  app: services/research_sandbox_service.open_session -> spawn worker
   v
research.sandbox_worker  (own process, import guard, lease + heartbeat)
   |  warm-up bars ............. GET  /api/v1/history         (OpenAlgo)
   |  live ticks -> bars ....... WebSocket proxy, Quote mode   (OpenAlgo)
   |  LiveEngine = backtest Engine with a live clock
   |  orders ................... POST /api/v1/researchsandbox  (app process)
   |                                  -> sandbox OrderManager, tagged research:<run>:<seq>
   v
ledger_orders / ledger_fills / ledger_trades   (shared with backtests)
ledger_events  (ORDER_INTENT, SUBMITTED, FILLED, REJECTED, UNFILLED, MARK, SESSION_EVENT)
research_session_bars (every bar the session saw)
```

**Why orders are placed in the app process.** The sandbox serialises position
updates with `ExecutionEngine._position_lock`, a lock that exists inside one
process. A second process placing sandbox orders would race the app's own on
the same position rows. So the worker asks the app, which places the order
under that lock.

**Why the worker is a separate process.** The same reasons as a backtest
(`57`): strategy code is CPU and arbitrary, and the app serves everything from
one eventlet worker. The worker is a client of OpenAlgo exactly like a hosted
Python strategy, with the deploying user's API key passed the way the strategy
host passes it.

## The engine, live

`research/live.py`. `LiveEngine` subclasses the backtest `Engine` and changes
two things: the clock (where a bar sits in its day is derived from the interval
and the 15:30 close, since a live session has no next bar) and execution (net
orders leave as `OrderIntent` objects with a reserved sequence number and come
back as fills). Books, decisions, stops and targets, the exposure limit,
netting and cost allocation are the backtest's code.

Parity is tested, not asserted: the same bars fed one at a time and filled
where a backtest fills give identical orders, fills, trades and equity in 24
randomized cases (`test_research_live.py`). The engine split itself was checked
byte-identical against the pre-split engine on 80 randomized backtests.

What genuinely differs in a real session is the market:

- **Bars are built from ticks** (`TickBarBuilder`), bucketed by when the
  session received each tick, aligned to IST midnight. They will not match the
  broker's own bars exactly. That is the cost of the WebSocket transport, chosen
  over pulling closed bars.
- **Fills are the sandbox's**: a MARKET order fills at its ask or bid from a
  live quote, at the moment the decision bar closes. A backtest fills at the
  next bar's open.
- **Slippage is observed, not modelled.** The modelled basis-point slippage is
  off in the Sandbox; each fill records its observed slippage against the
  decision price in its `ORDER_FILLED` event.

## A session's day

1. **Open** (scheduled start, or Start now): one row per deployment per day,
   enforced by a partial unique index. Skipped on days not scheduled or when the
   exchange is closed (`database.market_calendar_db.get_effective_session_window`).
2. **Claim and load**: lease taken; strategy versions checked against what is
   installed (a different version refuses to run); provenance recorded
   (PRD section 34).
3. **Warm up**: history from the start of `warmup_days` (5 by default, or a
   strategy's own `warmup_days`) up to now, seen by the strategies, never traded on.
4. **Trade**: at each closed bar the engine decides; each order is recorded as
   `ORDER_INTENT`, placed, and settled from the sandbox's answer.
   - a refusal leaves the book unchanged (`ORDER_REJECTED`)
   - an order still open after 20 seconds is cancelled (`ORDER_UNFILLED`). The
     sandbox can refuse that cancel (its margin release arithmetic does for some
     reducing orders), in which case the order stays tracked and is applied if
     it fills later. The sandbox fills a left-open order only while analyzer
     mode is on.
5. **Pause** stops new entries; exits still run. **Stale data** (no tick from
   any symbol for 2 minutes in market hours) pauses, or under `FAIL_SAFE`
   squares off and ends the session FAILED. **A failing strategy** (it raises,
   or answers with something the engine refuses, including a refused
   connection) is never asked again: the session squares off and ends FAILED
   with the cause, the same way. A failure while resuming waits until the
   books are rebuilt, so what is held is what gets closed.
6. **End**: the configured MIS exit time closes positions through the normal
   flow. A stop request, the stop time, or the market close squares off
   anything still held, then the session completes with metrics, attribution
   and an equity file. Positions that cannot be closed within a minute end the
   session FAILED naming them.

## Restart and recovery

- On SIGTERM the worker releases its lease. A crashed worker is restarted by
  the supervisor up to three times.
- At boot the app resumes today's interrupted sessions. Older ones are settled
  FAILED with a message pointing at the sandbox positions, because their day is
  over and their positions, if any, are still in the sandbox.
- A resuming worker rebuilds the books from the session's fills, rebuilds
  strategy state by replaying the session's stored bars through `decide` with
  the answers discarded, catches up on bars missed while down (seen, not
  traded), and settles every order decided before the restart by asking the
  sandbox by tag. An order that never reached the sandbox is not sent late: it
  was decided on a price that is now stale. Resuming past the exit time squares
  off at once.

## Decisions taken with the author

- **Sandbox everywhere.** The PRD says paper; this platform's word is Sandbox,
  in code (`environment = "SANDBOX"`), data and screens.
- **Scheduling follows the Python Strategy Host**: start and stop in IST on
  chosen days, holiday-aware.
- **WebSocket ticks** over pulling closed bars, accepting tick-built bars.
- **Resume, else square off** after a restart.

## Limits of this release

- **Intraday (MIS) only.** A deployment whose configurations are CNC is refused:
  a session is one trading day and carries nothing overnight.
- **Intervals from 1 minute to 1 hour.** Daily bars cannot be traded live.
- **Shadow and live** (PRD Phases 9 and later) are not built. The session's
  only execution door is the sandbox.
- **The sandbox's own behaviour applies**: one shared account and margin, its
  quote-based fills, its cancel refusals. Margin is checked by the sandbox on
  the shared account, not per strategy.
- **The boundary stops accidents, not code written to defeat it.** Strategy
  code runs in the worker's process, so code that sets out to get round the
  audit hook (a socket opened through `ctypes`, a connection the worker already
  holds, the `.env` file read from disk) is not stopped. Closing that needs the
  strategy in its own sandboxed process, which is not built. DuckDB opens files
  in C, outside the hook, so reading `historify.duckdb` directly is not refused
  either.
- **Not verified against a live market here.** Every UAT case runs on recorded
  or injected ticks, including over real HTTP through the real sandbox. Live
  ticks reaching a strategy and a real sandbox fill at market need a market
  session with a logged-in broker.
- **Session logs and equity files are kept indefinitely**, like backtests.

## Configuration

Existing keys only. Sessions reach OpenAlgo at `HOST_SERVER` and the WebSocket
proxy at `WEBSOCKET_HOST`/`WEBSOCKET_PORT`, with the deploying user's API key
(created on the API Key page; a session without one fails, saying so).

## Deployment

As for `57`: production on this machine runs the upstream image, so none of
this reaches the running instance until a fork image is built from `main`.
