# Verification of the research PRDs, item by item

Both PRDs checked against `feature/research-backtest` on 2026-10-11: the master
PRD ([`prd/research-platform.md`](../../prd/research-platform.md), 598 checklist
items in 47 sections) and the Sandbox PRD
([`prd/research-sandbox.md`](../../prd/research-sandbox.md), section 39's 29
acceptance criteria and its UAT cases).

**How.** Each item is traced to the test that pins it wherever one can, because
a test is a claim that is checked every run and a sentence is not; the code is
named only where no test can reach (a screen, a process step). Every test cited
was run on this date and passes. Tests added during this work were each seen
failing with the guard they depend on disabled before being trusted.

**Status words.** *Done*: built and pinned by the evidence. *Done (screen)*:
built in the UI; the evidence is the screen and the API test behind it. *Done,
with a note*: built, and the note says what it does not cover. *Handed off*:
belongs to another repository by the PRD's own rule. *Not built*: listed in
[Open items](#open-items).

**Evidence legend.** `name::test_x` is `test/research/test_research_<name>.py::test_x`
(`engine`, `live`, `live_door`, `registry`, `runs`, `sandbox_deploy`,
`sandbox_exec`, `sandbox_http`, `sandbox_session`, `support`);
`loss_limit::test_x` is `test/test_sandbox_loss_limit.py`; `*.test.tsx` are the
Vitest files under `frontend/src/pages/research/`. Design references are to
[57](../57-research-backtest/README.md), [58](../58-research-sandbox/README.md)
and [59](README.md).

## Master PRD

### 2. Strategy registry

| Item | Status | Evidence |
| --- | --- | --- |
| Create strategy registry; assign unique strategy IDs | Done | `registry::test_builtin_reference_strategy_is_registered_once`; `research_strategies.key` is unique |
| Create immutable strategy versions; prevent mutation of completed versions | Done | `registry::test_version_rows_refuse_to_be_edited` |
| Store strategy version, artifact hash, package version | Done | `registry::test_a_changed_artifact_is_a_new_version_and_the_old_one_is_kept` |
| Store model hash where applicable; store adapter version | Done | `registry::test_model_hash_and_adapter_version_join_identity_only_when_declared` |
| Store creation timestamp | Done | `research_strategy_versions.created_at`, shown on the strategy page |
| Store strategy metadata | Done | `meta` from the strategy's `metadata`; operator properties beside it: `registry::test_a_version_records_its_training_period_and_details_beside_it`, `registry::test_a_strategy_can_declare_its_training_period_and_the_operator_overrides_it` |

### 3. Strategy plugin system

| Item | Status | Evidence |
| --- | --- | --- |
| Python plugin interface; `decide(context)`; target-position representation | Done | `research/strategy.py` (`Strategy`, `Context`); `engine::test_a_buy_fills_at_the_next_open_and_exits_at_the_exit_time`, `engine::test_a_strategy_cannot_trade_outside_its_universe_or_over_its_capital` |
| Discovery/registration | Done | `openalgo.strategies` entry points; `registry::test_a_strategy_that_fails_to_load_is_reported_and_does_not_hide_others` |
| Plugin metadata | Done | `registry::test_a_strategy_can_declare_its_training_period_and_the_operator_overrides_it` |
| Lifecycle hooks; fill callback; session-end callback | Done | `engine::test_hooks_run_at_fixed_points_of_each_day`, `live::test_hooks_see_the_same_lifecycle_live_and_in_a_backtest` |
| Version plugin identity | Done | `research.strategy.version_identity`; `registry::test_a_changed_artifact_is_a_new_version_and_the_old_one_is_kept` |
| Test deterministic strategy execution | Done | `engine::test_identical_inputs_give_identical_ledgers`, `engine::test_each_instance_draws_from_its_own_seeded_generator` |
| Cannot access broker, sandbox | Done | `runs::test_a_strategy_that_reaches_for_order_placement_is_refused`, `runs::test_the_guard_covers_every_route_to_a_broker`, `sandbox_deploy::test_a_session_cannot_reach_any_order_path` |
| Cannot access database | Done | `sandbox_deploy::test_strategy_code_cannot_reach_the_order_api_over_http` (refuses database, process, thread) |
| Cannot access market-data transport | Done | `runs::test_strategy_code_in_a_backtest_worker_cannot_open_a_connection`, `runs::test_a_backtest_worker_refuses_the_network_for_its_whole_life` |
| Operates through Context; returns target positions, not orders | Done | `engine::test_the_strategy_cannot_see_future_bars_or_write_to_them`; `decide` returns weights, the engine makes orders |

### 4. Configuration system

| Item | Status | Evidence |
| --- | --- | --- |
| capital, allocation, leverage, universe, entry schedule, exit schedule, stop policy, take-profit policy | Done | `research.strategy.Config`; `registry::test_invalid_configs_are_refused_with_a_readable_reason` |
| risk limits | Done, with a note | Per position: stop and target in the configuration. Per portfolio: daily loss limit, profit target and lock floor on the scenario (`registry::test_scenario_portfolio_limits_are_validated_and_kept`) |
| Changes create new versions; old ones immutable; hash generated | Done | `registry::test_config_versions_grow_only_when_something_changed`, `registry::test_version_rows_refuse_to_be_edited` |
| Runs reference exact configuration version | Done | `runs::test_an_old_run_keeps_its_configuration_after_the_config_changes` |
| UI shows configuration version and which runs/sessions used it | Done (screen) | Strategy page, Configuration history; `StrategyDetail.test.tsx` |

### 6. Dataset system

| Item | Status | Evidence |
| --- | --- | --- |
| dataset ID, source, symbols, interval, start, end, row count, symbol count, first and last timestamp | Done | `research_datasets` columns; `registry::test_bars_come_from_the_provider_the_spec_names`; `support::test_the_migration_backfills_dataset_facts_from_their_snapshots` |
| dataset hash; snapshot identity; canonical fingerprint deterministic | Done | `support::test_dataset_hash_follows_the_bar_values_not_the_file` |
| Strategy cannot directly access DuckDB | Done, with a note | Bars only through `Context`; nothing stops plugin code opening the DuckDB file itself, because DuckDB opens files in C, outside the audit hook ([57](../57-research-backtest/README.md), Data) |
| Missing data explicit; no silent zero-filling | Done | `support::test_missing_data_is_refused_by_name_and_a_gap_is_never_filled` |
| Timestamp conventions consistent | Done | `live::test_bars_align_to_ist_midnight_not_utc` |
| Existing OpenAlgo historical-data infrastructure reused | Done | `HistorifyDuckDBProvider` reads Historify's store |

### 7. Experiment registry

| Item | Status | Evidence |
| --- | --- | --- |
| experiment ID, name, description, objective, timestamps | Done | `research_experiments`; the Research page form takes name, objective and description |
| scenarios; status; result aggregation | Done | `registry::test_an_experiment_states_its_objective_and_its_status_is_derived`; the experiment page aggregates its scenarios' runs |

### 8. Scenario system

| Item | Status | Evidence |
| --- | --- | --- |
| scenario ID, interaction mode, environment, dataset, universe, capital policy, allocation, leverage, cost model, strategy instances | Done | `research_scenarios`, `research_scenario_instances`; `runs::test_scenarios_refuse_mismatched_inputs` |
| execution model | Done, with a note | One model exists (`{"fill": "next_open"}`), recorded per scenario and in provenance; nothing to choose between yet |

### 9-12. Isolated, shared, conflicts, parallel scenarios

| Item | Status | Evidence |
| --- | --- | --- |
| 9: isolated market data, dataset, universe, execution assumptions, capital, positions, buying power, risk, constraints; independent cash, positions, P&L, risk state; no leakage | Done | `engine::test_isolated_books_cannot_affect_each_other`, `engine::test_isolated_limits_stop_only_the_strategy_that_lost`, `sandbox_session::test_isolated_books_never_net_and_shared_books_do` |
| 10: shared capital, buying power, positions, portfolio risk, exposure, environment, identity, intent, virtual books, attribution | Done | `engine::test_shared_mode_nets_opposite_intents_and_keeps_both_attributions`, `engine::test_shared_costs_go_to_the_books_that_carried_the_net_order`, `engine::test_shared_exposure_limit_scales_every_book_by_one_factor` |
| 11: same-direction and opposing signals, capital shortage, leverage and exposure limits, risk rejection | Done | the same engine tests; `engine::test_a_daily_loss_limit_squares_off_and_stops_new_entries_until_the_next_day` |
| 11: partial execution | Done | `live::test_a_partial_fill_gives_crossed_books_their_share_and_carriers_what_filled`, `sandbox_session::test_a_partly_filled_order_books_what_filled` |
| 12: unique scenario and run IDs, isolated mutable state, shared immutable data, concurrent execution, resource limits, independent failure, no contamination | Done | `runs::test_one_failing_scenario_does_not_touch_the_others`, `sandbox_session::test_concurrent_sessions_keep_their_own_books`, `runs::test_a_worker_gets_its_limits_after_exec_never_before` |

### 13-16. Engine, determinism, results, minimal ledger

| Item | Status | Evidence |
| --- | --- | --- |
| 13: strategy, version, config, dataset, universe, date range, capital, costs, slippage selection; single, isolated, shared modes | Done | `registry::test_a_strategy_backtests_from_its_registry_row_into_its_own_experiment`, `runs::test_isolated_and_shared_runs_complete_with_a_reconciled_ledger`, `support::test_intraday_costs_match_a_contract_note_by_hand` |
| 13: execution model | Done, with a note | As section 8 |
| 14: run hash, random seeds, engine version, dataset, config and strategy hash | Done | `runs::test_a_rerun_of_identical_inputs_returns_the_existing_result`, `engine::test_each_instance_draws_from_its_own_seeded_generator` |
| 15: run ID, status, duration, capital, final capital, gross and net P&L, return, drawdown, Sharpe, Sortino, win rate, profit factor, trade count, turnover, costs, exposure, artifacts | Done | `research/metrics.py`; `engine::test_run_metrics_report_capital_at_work_and_each_strategys_share`, `support::test_drawdown_uses_every_bar_and_cagr_needs_a_year`; the run page shows how long the run took |
| 16: SIGNAL, TARGET_POSITION, ORDER_INTENT, ORDER_SUBMITTED, ORDER_REJECTED, FILL, POSITION_UPDATE, FEE, SLIPPAGE, REALIZED_PNL, UNREALIZED_PNL, SESSION_EVENT, SQUARE_OFF | Done | `engine::test_the_ledger_records_each_decision_target_and_order_in_order`, `sandbox_session::test_a_session_journal_and_reconciliation_hold_after_a_day`, `sandbox_session::test_a_session_journal_names_the_venue_order` |

### 19. Artifact storage

| Item | Status | Evidence |
| --- | --- | --- |
| equity curves; Parquet files; DB records URI, hash, type, size | Done | `research_artifacts`; `runs::test_isolated_and_shared_runs_complete_with_a_reconciled_ledger` |
| score panels; feature panels; diagnostics (the per-symbol panel auditable) | Done | `ctx.record`: `runs::test_a_strategys_recorded_panel_is_kept_with_its_run_and_hashed`, `sandbox_session::test_a_session_keeps_what_its_strategy_recorded`, `runs::test_a_run_that_does_not_complete_leaves_no_records_file` |
| model artifacts | Done, with a note | The model's identity is stored (`artifact_hash`, `model_hash`); the file itself is the strategy package's, installed with it |

### 23-24, 27-29. Sandbox sessions

| Item | Status | Evidence |
| --- | --- | --- |
| 23: concurrent sessions, scenarios, portfolios, ledgers, instances | Done | `sandbox_session::test_concurrent_sessions_keep_their_own_books` |
| 24: simulated acceptance, fills, status, strategy capital and positions, allocation, portfolio P&L, attribution | Done | `sandbox_exec::test_an_order_fills_in_the_sandbox_and_a_retry_does_not_duplicate_it`, `sandbox_http::test_a_day_of_real_sandbox_orders_over_http`, `sandbox_session::test_a_single_strategy_trades_a_full_day_and_completes` |
| 27: startup, market open, EOD, square-off | Done | `sandbox_deploy::test_the_scheduler_holds_a_start_and_stop_job_per_active_deployment`, `sandbox_session::test_a_single_strategy_trades_a_full_day_and_completes`, `sandbox_session::test_stopping_mid_day_closes_positions_and_completes` |
| 27: data disconnect | Done | `sandbox_session::test_fail_safe_on_stale_data_flattens_and_fails`, `sandbox_session::test_paused_sessions_take_no_new_entries` |
| 27: sandbox disconnect | Done | `sandbox_session::test_the_session_says_when_its_venue_stops_answering` |
| 27: strategy crash | Done | `sandbox_session::test_a_strategy_that_fails_mid_day_is_squared_off_not_abandoned` |
| 27: platform restart; recovery | Done | `sandbox_deploy::test_boot_resumes_todays_sessions_and_settles_older_ones`, `sandbox_session::test_an_error_after_trading_never_ends_the_session_with_positions_open`, `sandbox_session::test_the_app_resumes_a_stopped_worker_then_closes_then_gives_up` |
| 27: order rejection | Done | `sandbox_session::test_a_rejected_order_leaves_the_book_flat` |
| 28: detect incomplete sessions; restore portfolio, positions, orders; reconcile; no duplicates; continue or terminate by policy | Done | `sandbox_session::test_a_restarted_session_resumes_without_a_duplicate_order`, `live::test_restore_rebuilds_books_from_recorded_fills`, `sandbox_session::test_a_strategy_that_fails_while_resuming_is_squared_off`, `sandbox_session::test_an_unknown_order_outcome_is_never_guessed_at` |
| 29: signals, targets, intents, fills, P&L parity with backtests | Done | `live::test_live_engine_matches_the_backtest_engine_on_the_same_bars`, `live::test_crossed_intents_settle_at_the_decision_close_live_and_in_a_backtest` |

### 30-31. Shadow and Live

| Item | Status | Evidence |
| --- | --- | --- |
| 30: same strategy, config, portfolio, ledger, attribution | Done | `sandbox_session::test_a_shadow_session_fills_inside_itself_and_sends_nothing_anywhere` |
| 31: same identity, configuration, portfolio, ledger, attribution | Done | `sandbox_session::test_a_live_session_sends_its_orders_through_the_live_door` |
| 31: LIVE environment; strategy enablement; capital limits; risk checks | Done | `live_door::test_a_live_deployment_passes_every_gate_and_a_typed_confirmation`, `live_door::test_a_live_session_stays_within_its_capital_limit`, `live_door::test_a_revoked_version_cannot_add_to_a_live_position` |
| 31: valid broker session | Done | Authorisation is resolved per order by `order_dispatch`; `live_door::test_a_refused_order_says_why_and_is_never_sent_again` |

### 32-37. Screens

| Item | Status | Evidence |
| --- | --- | --- |
| 32: strategy, version, status, configuration, last run, P&L; Backtest, Paper, Shadow, Live, Pause, Stop | Done (screen) | `StrategyActions.test.tsx`, `StrategyDetail.test.tsx`; `registry::test_the_registry_says_where_each_strategy_stands_and_how_it_last_did`, `registry::test_pause_and_stop_act_on_every_session_and_deployment_a_strategy_is_in` |
| 33: capital, allocation, leverage, universe, schedule, risk, environment, version, hash, creation time, usage history | Done (screen) | Strategy page configuration form and history; `StrategyDetail.test.tsx` |
| 34: create experiment, add strategy, choose version, config, dataset, mode; multiple scenarios; run concurrently | Done (screen) | Research and experiment pages; `runs::test_isolated_and_shared_runs_complete_with_a_reconciled_ledger` |
| 35: strategy, dataset, universe, date range, capital, cost model, slippage, mode, run button | Done (screen) | Experiment page scenario form |
| 36: Sandbox indicator, session ID, scenario, market-data and sandbox status, capital, P&L, positions, orders, fills, last signal | Done (screen) | Session page; `sandbox_session::test_the_session_says_when_its_venue_stops_answering`, `DeployToSandbox.test.tsx` |
| 37: strategy, scenario, isolated vs shared and configuration comparison; equity, drawdown, exposure, costs, attribution, overlap, conflict count, capital competition, netting impact, contribution | Done (screen) | `RunAnalytics.test.tsx`, `RunLedger.test.tsx`; `engine::test_netting_impact_counts_what_crossing_inside_the_portfolio_saved`, `engine::test_exposure_follows_what_each_book_holds`; `/research/compare/<a>/<b>` |

### 39. OpenAlgo LLD directive

| Item | Status | Evidence |
| --- | --- | --- |
| inspect architecture, blueprints, REST/API, services, DB, strategy host, sandbox, scheduler, market data, React, tests | Done | Recorded in [57](../57-research-backtest/README.md) and [58](../58-research-sandbox/README.md) before each was built |
| no new backend or frontend framework; no second broker abstraction, auth system, sandbox, market-data system, portfolio engine or ledger | Done | Flask blueprint and restx namespaces, React pages; orders through the sandbox's `OrderManager` and the strategy module's `order_dispatch`; risk through `services.risk`; bars from Historify and the WebSocket proxy; alerts through the Telegram alert service; fills through the event bus. One research ledger, shared by every environment |

### 43-46. Idempotency, resources, isolation, security

| Item | Status | Evidence |
| --- | --- | --- |
| 43: run creation | Done | `runs::test_a_rerun_of_identical_inputs_returns_the_existing_result` |
| 43: order creation | Done | `sandbox_exec::test_an_order_fills_in_the_sandbox_and_a_retry_does_not_duplicate_it`, `live_door::test_a_live_order_goes_to_the_live_pipe_once` |
| 43: fill processing; position update | Done | `live_door::test_a_stale_update_never_overwrites_the_fill_that_came_after_it`, `sandbox_session::test_a_restarted_session_resumes_without_a_duplicate_order` |
| 43: ledger creation | Done | `sandbox_session::test_a_session_ledger_holds_each_intent_once_and_before_it_was_sent` |
| 43: session completion | Done | `sandbox_deploy::test_one_session_a_day_however_often_it_is_started` |
| 43: EOD square-off | Done | `live::test_an_order_in_flight_is_not_ordered_twice` |
| 44: max concurrent backtests and scenarios, memory, CPU, job timeout; run states | Done | `runs::test_a_worker_gets_its_limits_after_exec_never_before`, `runs::test_a_backtest_past_its_time_limit_is_stopped_saying_why`, `runs::test_a_backtest_out_of_processor_time_is_told_so`, `sandbox_deploy::test_no_session_opens_past_the_servers_session_limit`, `runs::test_a_cancelled_run_never_becomes_completed` |
| 45: one scenario fails, others unaffected | Done | `runs::test_one_failing_scenario_does_not_touch_the_others` |
| 46: no new auth system | Done | Session login for screens, API keys for the doors (`live_door::test_a_live_session_belongs_to_its_user`) |
| 46: explicit LIVE mode; live action authorisation | Done | `live_door::test_live_is_off_until_the_server_allows_it`, `live_door::test_a_live_deployment_passes_every_gate_and_a_typed_confirmation` |
| 46: Paper, Backtest and Shadow safety boundaries | Done | `sandbox_exec::test_the_analyzer_switch_cannot_route_a_session_order_to_a_broker`, `live_door::test_a_sandbox_or_shadow_session_cannot_use_the_live_door`, `sandbox_deploy::test_a_session_worker_loads_nothing_that_can_place_an_order` |
| 46: broker credentials outside plugins | Done | `runs::test_workers_never_receive_the_broker_credentials` |

### 47-56. Phases

| Item | Status | Evidence |
| --- | --- | --- |
| 47: fork, baseline pinned, upstream tracking, baseline documented, suite run, broker, strategy host, sandbox, market data, frontend verified | Done | [57, Baseline](../57-research-backtest/README.md#baseline): `782adb740`, `upstream` = marketcalls/openalgo |
| 48-49: registry, versions, hashes, config versions and hashes, screens, validation, tests, UAT; experiments, scenarios, instances, datasets, snapshots, run registry, hashes, artifact references, status, provenance, screens | Done | Sections 2-8 above |
| 50: plugin contract, discovery, determinism, DatasetProvider, Historify provider, snapshots, hashing, BacktestContext, simulated clock and broker, modes, capital, allocation, leverage, positions, risk, conflicts, attribution, orders, fills, trades, P&L, scheduler, concurrency, limits, failure isolation | Done | Sections 3-16 and 43-45 above |
| 50: Alpha A adapter | Handed off | Belongs to algo-trading (PRD 1.3); [alpha-a-adapter.md](alpha-a-adapter.md) |
| 51: gate items | Done | [gate-review.md](gate-review.md) |
| 51: Alpha A parity | Handed off | The parity test is specified in the brief; it needs the adapter |
| 52: production ledger, order and fill lifecycle, trade reconstruction, reconciliation, attribution, cost accounting, audit tools | Done | Section 16 above; `sandbox_session::test_reconciliation_finds_a_fill_the_orders_do_not_explain`; journal CSV on the run page |
| 53: analytics, attribution, risk, exposure, capital utilisation, comparison, shared analytics, dashboards | Done | Section 37 above |
| 54: observability, resource management, recovery, operational controls, alerting, auditability, performance, security hardening | Done | [59, Step 7](README.md#step-7-hardening); `sandbox_session::test_session_log_lines_carry_the_ids_they_are_searched_by`, `sandbox_session::test_alerts_reach_telegram_only_as_flow_sends_them`, `sandbox_session::test_the_operations_panel_lists_each_worker_with_its_memory_and_lease`, `runs::test_run_logs_past_their_retention_are_pruned_once_a_day` |
| 55: Sandbox phase items | Done | Sections 23-29 above |
| 56: Shadow: live data and strategy, no live orders, observational execution, ledger, comparison | Done | `sandbox_session::test_a_shadow_session_fills_inside_itself_and_sends_nothing_anywhere`, `sandbox_session::test_shadow_compares_with_the_sandbox_session_of_the_same_day` |
| 56: Live: adapter, broker execution, LIVE environment, risk controls, capital limits, kill switch, reconciliation, ledger, monitoring | Done | Section 31 above; `live_door::test_engaging_the_kill_switch_stops_every_live_session`, `sandbox_session::test_the_kill_switch_stops_a_live_session_and_closes_its_positions`, Live panel's check against the broker, `LivePanel.test.tsx` |

### 57, 59, 61, 63, 66. UAT, parity, matrix, discipline, final checklist

These repeat earlier items; each maps to the section shown.

| Item | Status | Where |
| --- | --- | --- |
| 57 Configuration (5), 66 Strategy and Configuration | Done | Sections 2-4 |
| 57 Backtest (7), 66 Backtest and Portfolio | Done | Sections 9-15 |
| 57 Parallel execution (4) | Done | Section 12 |
| 57 Ledger (5), 66 Ledger | Done | Section 16, 52 |
| 57 Paper (9), 66 Paper | Done | Sections 23-29 |
| 57 and 66 Shadow, Live | Done | Sections 30-31, 56 |
| 57 and 66 Safety: Backtest, Paper, Shadow cannot reach broker; global switch cannot bypass; Live explicit | Done | Section 46 |
| 59 signals, targets, orders, fills, positions, P&L parity | Done | Section 29 |
| 61 unique identity, immutable configuration, no leakage, separate attribution | Done | Sections 4, 9, 12 |
| 63 inspect, reuse, interfaces, schema, tests, implement, integration tests, UAT, provenance audit, commit, document | Done | Each phase: design doc first, tests with it, migration, changelog; provenance on every run (`runs::test_a_run_keeps_its_strategys_properties_as_they_were_when_it_ran`) |
| 66 Foundation, Data, Research, UAT, Architecture | Done | Sections 6-8, 39, 47 |
| 66 Strategy: Alpha A adapter, decision parity | Handed off | [alpha-a-adapter.md](alpha-a-adapter.md) |

## Sandbox PRD, section 39 acceptance criteria

| Item | Status | Evidence |
| --- | --- | --- |
| Existing strategy plugin runs unchanged; identity and configuration preserved | Done | `live::test_live_engine_matches_the_backtest_engine_on_the_same_bars`; sessions record the same version and configuration ids |
| Live data reaches PaperContext; strategy cannot touch the transport | Done | `live::test_ticks_become_bars_on_the_interval`; section 3 refusals |
| Paper uses sandbox; cannot invoke live broker | Done | `sandbox_http::test_a_day_of_real_sandbox_orders_over_http`, `sandbox_exec::test_the_analyzer_switch_cannot_route_a_session_order_to_a_broker` |
| Orders and fills correlate | Done | `sandbox_session::test_a_session_journal_names_the_venue_order` |
| Isolated and shared modes work; virtual capital enforced | Done | `sandbox_session::test_isolated_books_never_net_and_shared_books_do`, `sandbox_http::test_opposite_intents_in_a_shared_session_send_nothing_to_the_sandbox` |
| Positions reconcile; signals, orders, fills recorded; P&L reconciles; attribution works | Done | `sandbox_session::test_a_session_journal_and_reconciliation_hold_after_a_day` |
| Session lifecycle; EOD; restart/recovery; failures recoverable | Done | Sections 27-28 above |
| Multiple scenarios simultaneously; no contamination; one can fail without corrupting others | Done | `sandbox_session::test_concurrent_sessions_keep_their_own_books`, `runs::test_one_failing_scenario_does_not_touch_the_others` |
| BACKTEST, PAPER cannot reach broker; global switch cannot bypass | Done | Section 46 above |

UAT PAPER-001 to PAPER-012 are named in the docstrings of the tests that carry
them (`grep -rn "PAPER-0" test/research/`).

## Open items

- **Alpha A adapter and decision parity** (sections 50, 51, 66): algo-trading's,
  by the PRD's own rule; [the brief](alpha-a-adapter.md) says what it needs.
- **Strategy code could open the DuckDB file itself** (section 6): the guard
  cannot see a C-level open; it is the contract's rule, not the guard's.
- **One execution model** (sections 8, 13): next bar's open. A second one would
  be a scenario option; none has been asked for.
- **Not deployable yet**: production runs the upstream OpenAlgo image; none of
  this reaches it until the fork's own image is built and pinned.
