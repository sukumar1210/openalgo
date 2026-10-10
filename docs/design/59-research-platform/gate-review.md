# Phase 4 Architectural Gate Review

The review the master PRD asks for before any phase after 4 is planned in detail
([`research-platform.md`](../../prd/research-platform.md), section 51). It is
written late: Phase 8 (the Sandbox) was built before it, at the author's
direction, so it also records what Phase 8 taught. Everything here is on branch
`feature/research-backtest` as of 2026-10-10 (`d5b498cba`).

## Verdicts

| Item | Verdict | Evidence |
| --- | --- | --- |
| Plugin abstraction | Holds, needs lifecycle hooks | `decide(ctx)` returning signed weights carried every strategy written so far, unchanged from backtest to Sandbox. The PRD contract also names `metadata()`, `initialize()`, `on_fill()` and `on_session_end()`; none exist yet. |
| Alpha A parity | **Not reviewable** | The Alpha A adapter does not exist. It belongs in algo-trading (PRD 1.3), so this gate item stays open until it is written there and a parity run compares it with Alpha A's own backtester. |
| Data provider | Holds, abstraction missing | Historify DuckDB through one loader, snapshots pinned per scenario and hashed by bar values (`test_dataset_hash_follows_the_bar_values_not_the_file`). There is no `DatasetProvider` seam, and the dataset row lacks source, symbol count and first and last timestamps. |
| Isolated simulation | Holds | `test_isolated_books_cannot_affect_each_other`; isolated and single are the same computation by construction (one portfolio per book). |
| Shared simulation | Holds | `test_shared_mode_nets_opposite_intents_and_keeps_both_attributions`, `test_shared_costs_go_to_the_books_that_carried_the_net_order`, `test_shared_exposure_limit_scales_every_book_by_one_factor`. |
| Portfolio semantics | Holds | Same engine in backtest and Sandbox; parity in 24 randomized cases (`test_research_live.py`). |
| Conflict resolution | Holds for netting, gaps at the edges | `net-v1`: costs on the net order, pro rata to the books that carried it. No portfolio loss limit (risk rejection), and partial execution is not modelled. |
| Ledger reconciliation | Holds | Trade P&L plus costs equals equity change per book, and attribution sums to the portfolio, in every run test (`reconcile`, `reconciles`). |
| Deterministic reruns | Holds | `test_identical_inputs_give_identical_ledgers`; a rerun of identical inputs returns the stored result (`run_hash`, partial unique index). No seed is recorded because the engine draws no random numbers; a strategy that does has nothing to seed it. |
| Performance | Adequate | One year of 5-minute bars, 20 symbols, two strategies: 2.5 s (about 7,600 bars a second) in either mode, on this machine. A run is one process; two run at once by default. |
| UAT | Partly automated | Section "UAT status" below. |

## Assumptions validated

- **A strategy needs only bars, its own position and its own equity** to decide.
  Nothing written so far asked for more.
- **Isolated and shared differ only in grouping.** One engine code path serves
  both; the tests that pin isolation also pin that the grouping is the only
  difference.
- **Netting at the portfolio is enough to model shared execution.** A shared
  portfolio sends one net order; each book still holds its own decision and its
  own P&L.
- **Subprocess workers keep the single app worker responsive.** CPU work never
  runs on the eventlet hub; memory and descriptor limits hold per run.
- **The same engine can run live.** Splitting `step_decisions` out of the loop
  was checked byte-identical against the old engine on 80 randomized backtests,
  and the live engine changed only the clock and where fills come from.
- **The sandbox can stand in for a broker** for MARKET orders: it fills at bid
  or ask from a live quote, at once, and reports a final state.

## Assumptions invalidated

- **"The import guard is the broker boundary."** It was not: strategy code could
  reach `/api/v1/placeorder` over HTTP with the key the worker held, and that
  endpoint follows the analyzer switch. Fixed by refusing network, database,
  process and thread while strategy code runs, and taking the key out of the
  environment (`07abdc487`).
- **"A worker's environment is harmless."** It carried the broker's credentials
  and the app's keys. Fixed (`d5b498cba`).
- **"A strategy error ends a run cleanly."** In a backtest it does; in a session
  it left positions in the sandbox with nothing watching them. Fixed by squaring
  off first (`e5acaa313`).
- **"Order identity can be assigned at fill time."** A live order needs its id
  before it is sent; the backtest assigns it at the fill. The two numbering
  schemes differ, and the parity test normalises around it. Phase 5 reserves the
  id at the decision everywhere.
- **"A sandbox cancel always succeeds."** Its margin release arithmetic refuses
  some cancels of reducing orders; such orders are tracked to a final state
  rather than assumed cancelled.

## Limitations carried forward

- Stops are checked on the bar's close and exit at the next open; no intrabar
  fills.
- Partial fills are not modelled in the backtest, and a partly filled live order
  needs handling the sandbox never exercised.
- Bars built from live ticks differ from the broker's own bars.
- The strategy boundary is in-process: it stops accidents, not code written to
  defeat an audit hook. DuckDB, which opens files in C, is not refused.
- No live market has been traded on a fork build.
- `CNC` positions cannot be carried across Sandbox sessions.

## Required architectural changes

These are the seams the later phases need; each is planned in the
[README](README.md).

1. **Order identity at the decision, in every environment** (`research:<run>:<seq>`
   reserved when the order is decided). Needed for one ledger across
   environments. Changes backtest sequence numbers, so `ENGINE_VERSION` moves.
2. **One ledger journal for every environment**: the events the PRD lists
   (section 16), persisted where they are facts nobody else holds (signals,
   targets, order intents, square-offs, position snapshots) and derived where a
   typed table already holds them (fills, fees, slippage, realized P&L).
3. **Exposure in the engine**, recorded per bar beside equity, for capital
   utilisation, exposure and overlap analytics.
4. **A portfolio loss limit through `services.risk.evaluate_aggregate`**, the
   shared risk core, not a second evaluator.
5. **An execution adapter seam in the session**: the Sandbox door, a Shadow
   simulation inside the worker, and a Live door through OpenAlgo's existing
   live order path. The session, engine and ledger stay one.
6. **Strategy lifecycle hooks and a recorded seed**, so a strategy can hold
   state across a day and use randomness reproducibly.

## UAT status (PRD section 57)

| Area | Case | Automated in |
| --- | --- | --- |
| Configuration | registration, versioning, creation, immutability | `test_research_registry.py` |
| Backtest | single, deterministic rerun, isolated and shared A/B, opposing signals, capital constraints, attribution | `test_research_engine.py`, `test_research_runs.py` |
| Parallel | multiple scenarios, concurrent runs, state and failure isolation | `test_research_runs.py` (`test_one_failing_scenario_does_not_touch_the_others`) |
| Ledger | order, fill, trade, P&L reconciliation, attribution | every run test reconciles |
| Sandbox | sandbox fills, isolated, shared, concurrent, restart, EOD, rejection, no broker access | `test_research_sandbox_*.py`, PAPER-001 to PAPER-012 |
| Sandbox | **live data** | not run: needs market hours and a fork build |
| Safety | backtest and Sandbox cannot reach a broker, the global switch cannot bypass it | `test_research_runs.py`, `test_research_sandbox_deploy.py`, `test_research_sandbox_exec.py` |
| Safety | Shadow cannot reach a broker; Live needs explicit mode | Phase 9 |
| Matrix | 50/50, 60/40, 70/30 in both modes at once (section 61) | not automated yet |

## Gate decision

The Phase 4 architecture is kept. It needs the six changes above, and none of
them replaces a component. One gate item stays open: **Alpha A parity**, which
needs the adapter in algo-trading. Later phases go ahead on the reference
strategy and synthetic fixtures, and Alpha A parity becomes a release gate for
Live instead of a gate on building it.
