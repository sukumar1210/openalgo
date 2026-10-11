# Multi-Strategy Algo Trading Platform

## Product Requirements Document — PRD v1.0

> **Fork note.** This PRD belongs to the `sukumar1210/openalgo` fork. It is kept
> as written by its author. Two reading rules apply on this platform:
>
> - **Vocabulary.** Where this document says *paper* (paper trading, paper
>   sessions, `PAPER` mode) the platform's word is **Sandbox**, and nothing on a
>   screen says "paper". `SHADOW` (decide, record, send nothing) and `LIVE` keep
>   their names. See the vocabulary rules in `CLAUDE.md`.
> - **What is built.** Phases 1-4 are implemented as **Research** (`/research`).
>   The design, the decisions taken against this document, and what is deferred
>   are in [`docs/design/57-research-backtest/README.md`](../design/57-research-backtest/README.md).

**Status:** Proposed
**Primary implementation:** Fork of OpenAlgo
**Execution broker:** Existing OpenAlgo broker integrations
**Initial broker:** Zerodha Kite
**Primary market:** Indian equities
**Primary timezone:** IST
**Document purpose:** Define the product, architecture, simulation modes, configuration system, persistence, provenance, UAT, HLD, and LLD directives for extending OpenAlgo into a multi-strategy research, backtesting, paper-trading, and live-trading platform.

---

# 1. Executive Summary

The platform will extend OpenAlgo into a **multi-strategy algorithmic trading platform** capable of:

1. Registering and versioning strategies.
2. Configuring strategies from the UI.
3. Running historical backtests.
4. Persisting complete backtest run metadata and results.
5. Running multiple strategies simultaneously.
6. Comparing strategies under identical market conditions.
7. Simulating strategies independently using isolated capital.
8. Simulating strategies together in a shared portfolio/environment.
9. Running multiple portfolio configurations concurrently.
10. Running paper/shadow/live strategies through OpenAlgo execution.
11. Maintaining strategy-level and portfolio-level ledgers.
12. Preserving complete provenance for every result and trade.
13. Providing a common UI for configuration, execution, backtesting, comparison, and analytics.

The platform is **not intended to replace OpenAlgo's execution engine**.

OpenAlgo remains the execution substrate, broker integration layer, market-data layer, strategy-hosting foundation, and operational UI foundation.

Our additions provide the **research, portfolio, experiment, simulation, configuration, and attribution layer** around it.

---

# 2. Product Vision

The end state is:

```text
                 STRATEGY REGISTRY
                        │
                        ▼
                STRATEGY VERSION
                        │
                        ▼
                STRATEGY CONFIG
                        │
                        ▼
               STRATEGY INSTANCE
                        │
              ┌─────────┴─────────┐
              │                   │
          BACKTEST             LIVE/PAPER
              │                   │
              ▼                   ▼
        SIMULATION            OPENALGO
              │                   │
              └─────────┬─────────┘
                        ▼
                 PORTFOLIO ENGINE
                        │
                        ▼
                     LEDGER
                        │
                        ▼
                       DB
                        │
                        ▼
                    ANALYTICS
```

A strategy should be able to move through:

```text
Research
   ↓
Backtest
   ↓
Competition
   ↓
Portfolio simulation
   ↓
Paper
   ↓
Shadow
   ↓
Live
```

without losing its identity or provenance.

---

# 3. Core Product Concepts

## 3.1 Strategy

A logical trading strategy.

Example:

```text
Alpha A
Alpha B
Alpha C
```

A strategy is not mutable research state.

---

## 3.2 Strategy Version

Immutable version of the strategy implementation/model.

Example:

```text
Alpha A
  v0.2.5
```

A strategy version must have:

- source/artifact hash
- model hash where applicable
- creation timestamp
- strategy metadata
- compatible configuration schema
- implementation version

A completed run must reference a specific strategy version.

---

# 4. Strategy Configuration

Configuration is separate from strategy implementation.

Examples:

```text
Capital
Allocation
Leverage
Entry time
Exit time
Stop-loss policy
Take-profit policy
Universe
Risk limits
Execution mode
```

Configuration is controlled through the UI.

Example:

```text
Alpha A
────────────────────────

Capital:             ₹5,00,000
Allocation:          100%
Leverage:            1.0x

Entry:               09:16
Exit:                EOD

Stop:                OFF
Take Profit:         OFF

Max Position:        ₹5,00,000
Max Daily Loss:      ₹10,000

Execution:           PAPER
```

---

# 5. Configuration Versioning

Configurations are immutable once used by a run.

Changing:

```text
Leverage 1x → 2x
```

creates a new configuration version.

Example:

```text
Alpha A
  Strategy Version: v0.2.5

  Config v12
    leverage = 1.0

  Config v13
    leverage = 2.0
```

Every run records:

```text
strategy_version_id
config_version_id
config_hash
```

This prevents historical results from silently changing when the UI configuration changes.

---

# 6. Research Parameters vs Deployment Parameters

The UI must distinguish between:

### Deployment configuration

Allowed:

- capital
- allocation
- leverage
- execution mode
- trading schedule
- risk limits
- enable/disable
- portfolio membership

### Research/model parameters

Not freely exposed as deployment knobs unless explicitly registered as an experiment.

Examples:

- feature selection
- model architecture
- training parameters
- thresholds
- target construction
- feature transformations

These must remain part of a versioned strategy/model artifact.

This prevents the UI from becoming an uncontrolled optimization interface.

---

# 7. Multi-Strategy System

The platform supports multiple strategy instances simultaneously.

Each strategy instance has:

```text
strategy_id
strategy_version_id
config_version_id
scenario_id
capital allocation
status
```

Example:

```text
Experiment 104

Strategy Instance A
  Alpha A
  v0.2.5
  Config v12
  ₹5L

Strategy Instance B
  Alpha B
  v0.1.0
  Config v7
  ₹5L
```

---

# 8. Multi-Strategy Mode A — Isolated Parallel Worlds

## Definition

Each strategy operates in an independent simulated environment.

The strategies share:

- market data
- historical period
- universe
- cost model
- execution assumptions
- clock

They do not share:

- capital
- positions
- orders
- fills
- portfolio constraints
- exposure

Example:

```text
                    NIFTY 500
                       │
             ┌─────────┴─────────┐
             │                   │
        WORLD A               WORLD B
             │                   │
          Alpha A             Alpha B
             │                   │
           ₹5L                 ₹5L
             │                   │
       independent         independent
        positions            positions
```

If Alpha A buys RELIANCE, Alpha B can independently buy or sell RELIANCE.

## Purpose

This answers:

> Which strategy performs better when each receives the same starting resources?

This is the primary **strategy competition mode**.

---

# 9. Multi-Strategy Mode B — Shared Universe / Shared Portfolio

## Definition

All strategies operate inside one simulated portfolio environment.

They share:

- capital
- universe
- market data
- positions
- exposure limits
- portfolio constraints
- execution environment
- available buying power

Example:

```text
                 ₹5,00,000
                     │
             Shared Portfolio
                     │
          ┌──────────┴──────────┐
          │                     │
       Alpha A               Alpha B
          │                     │
       signals               signals
          │                     │
          └──────────┬──────────┘
                     ▼
             Portfolio Engine
                     │
              Net Positions
                     │
                Execution
```

Example:

```text
Alpha A → BUY RELIANCE 500
Alpha B → BUY RELIANCE 300

Portfolio:
RELIANCE = +800
```

Another case:

```text
Alpha A → BUY RELIANCE 500
Alpha B → SELL RELIANCE 500

Portfolio:
RELIANCE = 0
```

However, the ledger must preserve both strategy intents and attribution.

---

# 10. Shared Portfolio Conflict Resolution

The shared environment must explicitly define:

1. signal collection
2. desired-position calculation
3. capital allocation
4. conflict resolution
5. risk checks
6. netting
7. simulated execution
8. fill allocation
9. strategy attribution

Pipeline:

```text
Strategy Signals
       ↓
Desired Positions
       ↓
Portfolio Allocation
       ↓
Conflict Resolution
       ↓
Risk / Exposure Checks
       ↓
Net Desired Positions
       ↓
Execution Simulation
       ↓
Fills
       ↓
Strategy Attribution
       ↓
Ledger
```

The conflict-resolution policy must be deterministic and versioned.

---

# 11. Scenario System

A scenario is a complete simulation environment.

A scenario contains:

```text
scenario_id
mode
market dataset
universe
capital policy
cost model
execution model
risk policy
strategy instances
```

Two modes:

```text
ISOLATED
SHARED
```

---

# 12. Multiple Configurations Running in Parallel

An experiment can contain multiple scenarios.

Example:

```text
Experiment #104

Scenario 1
  ISOLATED
  Alpha A ₹5L
  Alpha B ₹5L

Scenario 2
  SHARED
  Alpha A ₹5L
  Alpha B ₹5L

Scenario 3
  SHARED
  Alpha A ₹3L
  Alpha B ₹2L

Scenario 4
  SHARED
  Alpha A ₹2.5L
  Alpha B ₹2.5L

Scenario 5
  ISOLATED
  Alpha A 2x
  Alpha B 1x
```

All scenarios use the same market dataset and can execute concurrently.

The platform must make it possible to compare them as one experiment.

---

# 13. Experiment

An experiment is a collection of scenarios being evaluated together.

Example:

```text
Experiment #104

Objective:
Compare Alpha A and Alpha B

Dataset:
NSE-EOD-2023-2026

Universe:
NIFTY 500

Cost Model:
TO-1

Scenarios:
5

Status:
COMPLETED
```

Experiment-level comparison:

```text
Scenario      Return    Max DD    Sharpe    Trades
----------------------------------------------------
S1            +12.3%    -8.2%     1.31      739
S2            +17.1%    -7.4%     1.48      812
S3            +15.2%    -6.9%     1.44      798
...
```

---

# 14. Backtesting

The platform must provide a first-class backtesting module.

Backtest inputs:

```text
Strategy
Strategy Version
Configuration
Dataset
Universe
Date Range
Capital
Cost Model
Slippage Model
Execution Model
Interaction Mode
```

Backtest output:

```text
Run ID
Performance metrics
Trade ledger
Equity curve
Drawdown
Exposure
Turnover
Costs
Per-strategy attribution
Artifacts
```

---

# 15. Single-Strategy Backtest

Example:

```text
Strategy:
Alpha A

Version:
0.2.5

Capital:
₹5,00,000

Universe:
NIFTY 500

Date:
2023-01-01 → 2026-09-30

Mode:
Single
```

Results:

```text
Net Return
CAGR
Net P&L
Max Drawdown
Sharpe
Sortino
Win Rate
Profit Factor
Trade Count
Average Trade
Costs
Turnover
```

---

# 16. Multi-Strategy Backtest

Example:

```text
Experiment
   │
   ├── Scenario A
   │     ├── Alpha A
   │     └── Alpha B
   │
   ├── Scenario B
   │     ├── Alpha A
   │     └── Alpha B
   │
   └── Scenario C
         ├── Alpha A
         └── Alpha B
```

The execution scheduler should run scenarios independently but use a common immutable market-data snapshot.

---

# 17. Deterministic Backtesting

For identical:

```text
strategy version
configuration
dataset
universe
cost model
execution model
```

the backtest must produce deterministic results unless explicitly configured otherwise.

A `run_hash` should be generated from the complete run specification.

Example:

```text
run_hash =
SHA256(
    strategy_version +
    config +
    dataset_hash +
    universe +
    cost_model +
    execution_model +
    interaction_mode
)
```

This prevents accidental duplication and makes results reproducible.

---

# 18. Common Strategy Interface

The same strategy logic should be usable across:

```text
BACKTEST
PAPER
SHADOW
LIVE
```

Conceptually:

```python
strategy.on_market_data(context)
strategy.generate_signal(context)
```

The context changes.

### Backtest

```text
HistoricalDataProvider
SimulatedClock
SimulatedBroker
```

### Live

```text
LiveDataProvider
LiveClock
OpenAlgoExecutionAdapter
```

The strategy must not directly depend on the real broker.

---

# 19. OpenAlgo Integration

OpenAlgo remains responsible for:

- broker connectivity
- broker authentication
- order execution
- market-data connectivity
- existing strategy hosting
- scheduling foundations
- existing sandbox functionality
- operational trading UI

OpenAlgo already provides a Python strategy management system with isolated strategy processes, scheduling, logs, parameter configuration, and exchange-aware scheduling.

Its architecture also already separates broker adapters, services, databases, strategy hosting, and frontend/backend surfaces.

Our system should therefore extend these mechanisms rather than duplicate them.

---

# 20. Execution Adapter

Create a thin abstraction:

```text
OpenAlgoExecutionAdapter
```

Responsibilities:

- translate portfolio orders into OpenAlgo orders
- capture OpenAlgo order IDs
- receive order status
- receive fills
- normalize execution events
- associate events with strategy instances

OpenAlgo order IDs are execution references.

They are **not** the primary research/strategy trade identity.

---

# 21. Ledger

The platform requires a first-class ledger.

Every relevant event should carry:

```text
strategy_id
strategy_version_id
config_version_id
experiment_id
scenario_id
session_id
signal_id
order_id
fill_id
trade_id
symbol
side
quantity
requested_price
fill_price
fees
slippage
gross_pnl
net_pnl
timestamp
```

The ledger must support:

- strategy P&L
- portfolio P&L
- realized P&L
- unrealized P&L
- transaction costs
- attribution
- capital usage
- exposure
- trade reconstruction

---

# 22. Database Requirements

The relational database should contain metadata and transactional records.

Core entities:

```text
strategies
strategy_versions
strategy_configs

experiments
scenarios
scenario_strategy_instances

datasets
dataset_versions

backtest_runs
backtest_trades

orders
fills
positions
ledger_entries

live_sessions
paper_sessions
```

---

# 23. Large Artifact Storage

Large analytical artifacts must not unnecessarily be stored as relational rows.

Examples:

```text
feature panels
score panels
equity curves
large diagnostic datasets
model artifacts
parquet files
```

Store them as files/artifacts and keep:

```text
artifact_uri
artifact_hash
artifact_type
artifact_size
created_at
```

in the database.

This preserves the existing research discipline that complete per-symbol score/prediction panels must remain auditable.

---

# 24. Database Isolation

Follow OpenAlgo's existing database architecture.

OpenAlgo already separates concerns across databases including the main application DB, logs, latency, health, sandbox, and historical-data DuckDB.

The extension should not collapse everything into one database.

Recommended initial separation:

```text
openalgo.db
    application / strategy / configuration metadata

research.db
    experiments
    scenarios
    backtest runs
    strategy comparisons

ledger.db
    orders
    fills
    trades
    positions
    ledger entries

sandbox.db
    simulated execution state

historify.duckdb
    historical market data
```

If the existing OpenAlgo schema provides a better natural home for an entity, reuse it rather than creating a duplicate.

---

# 25. UI Requirements

## Strategy Registry

Display:

```text
Strategy
Version
Status
Current Config
Mode
Last Run
P&L
```

Actions:

```text
View
Configure
Backtest
Paper
Shadow
Live
Pause
Stop
```

---

# 26. Configuration UI

The configuration UI must provide:

```text
Strategy
Version
Capital
Allocation
Leverage
Universe
Entry
Exit
Risk
Execution mode
```

The UI must show:

```text
Current Version
Last Modified
Used By
```

Changing a configuration creates a new version.

---

# 27. Backtest UI

Backtest creation:

```text
Strategy / Strategies
Version
Dataset
Universe
Start Date
End Date

Mode:
  Single
  Isolated
  Shared

Capital

Cost Model
Slippage Model

[ RUN ]
```

For multi-strategy experiments:

```text
+ Add Scenario
```

Each scenario can have independent configuration.

---

# 28. Run Dashboard

Every run should have:

```text
Run ID
Status
Duration
Strategy
Scenario
Mode
Dataset
Config
Return
P&L
Max DD
Sharpe
Trades
Costs
```

Actions:

```text
View Trades
View Equity
View Drawdown
View Attribution
View Configuration
View Artifacts
Re-run
Compare
```

---

# 29. Comparison UI

Comparison must support:

```text
Strategy vs Strategy
Scenario vs Scenario
Configuration vs Configuration
Isolated vs Shared
```

Metrics:

```text
Return
CAGR
P&L
Max DD
Sharpe
Sortino
Win Rate
Profit Factor
Trade Count
Turnover
Costs
Capital Utilization
```

For shared portfolios additionally:

```text
Correlation
Overlap
Conflict Rate
Capital Competition
Netting Impact
Contribution by Strategy
```

---

# 30. Strategy Attribution

In shared mode, the system must answer:

> How much did each strategy contribute to the final portfolio?

At minimum:

```text
Gross contribution
Net contribution
Costs
Capital consumed
Trades
Winning trades
Losing trades
Position overlap
Conflict count
Orders reduced/rejected because of portfolio constraints
```

---

# 31. Configuration Matrix

The platform should support matrix experiments.

Example:

```text
Strategy A:
    leverage = [1x, 1.5x, 2x]

Strategy B:
    allocation = [25%, 50%, 75%]

Modes:
    [isolated, shared]
```

The experiment planner generates scenarios:

```text
A1/B1/isolated
A1/B1/shared
A1/B2/isolated
A1/B2/shared
...
```

Each generated scenario must have an explicit configuration record.

No hidden combinations.

---

# 32. Parallel Execution

Multiple scenarios must execute concurrently.

Requirements:

- isolated process/job context
- deterministic dataset access
- independent state
- independent failure handling
- centralized run tracking
- resource limits
- progress reporting

Failure of one scenario must not corrupt another scenario's state.

---

# 33. Resource Management

The scheduler must prevent unlimited parallel runs.

Configuration:

```text
max_concurrent_backtests
max_concurrent_scenarios
max_memory_per_run
max_cpu_per_run
```

The system must expose:

```text
QUEUED
RUNNING
COMPLETED
FAILED
CANCELLED
```

---

# 34. Provenance Requirements

Every result must be reproducible from:

```text
Strategy version
Config version
Dataset version
Universe version
Cost model
Execution model
Interaction mode
Engine version
```

A result without complete provenance is **not a valid research result**.

---

# 35. Research Integrity Rules

The platform must enforce:

1. No mutation of completed runs.
2. No mutation of completed configurations.
3. No mutation of strategy versions.
4. Dataset hashes recorded.
5. Model hashes recorded.
6. Configuration hashes recorded.
7. Engine version recorded.
8. Full score/prediction panels persisted where applicable.
9. Random seeds persisted where applicable.
10. Failed runs remain visible.
11. Cancelled runs remain visible.
12. Results cannot be silently overwritten.

---

# 36. UAT Strategy

UAT is a mandatory product gate.

A feature is not considered complete merely because unit/integration tests pass.

UAT must verify:

```text
UI
→ API
→ Service
→ DB
→ Simulation
→ Ledger
→ Analytics
```

as an end-to-end workflow.

---

# 37. UAT Phase 1 — Strategy Configuration

### UAT-001

Create strategy:

```text
Alpha A v1
```

Expected:

- strategy appears in registry
- version exists
- artifact hash exists

### UAT-002

Create configuration:

```text
₹5L
1x
09:16
EOD
```

Expected:

- config version created
- config hash created

### UAT-003

Change leverage:

```text
1x → 2x
```

Expected:

- new config version
- previous config unchanged
- previous runs still reference old config

---

# 38. UAT Phase 2 — Single Backtest

Run Alpha A.

Verify:

- run created
- run ID generated
- strategy version recorded
- config version recorded
- dataset recorded
- run hash generated
- metrics persisted
- trades persisted
- artifacts persisted

Re-run identical configuration.

Expected:

```text
identical inputs
→ identical result
```

---

# 39. UAT Phase 3 — Isolated Competition

Configure:

```text
Alpha A = ₹5L
Alpha B = ₹5L
Mode = ISOLATED
```

Both receive the same universe and market data.

Expected:

- separate capital
- separate positions
- separate orders
- separate P&L
- identical market environment
- one strategy cannot alter the other's state

A BUY by A must not affect B's simulated position.

---

# 40. UAT Phase 4 — Shared Competition

Configure:

```text
Alpha A = ₹5L
Alpha B = ₹5L
Mode = SHARED
```

Expected:

- one portfolio
- common buying power
- common positions
- strategy-level attribution

Conflict test:

```text
A → BUY 100 RELIANCE
B → SELL 100 RELIANCE
```

Expected:

- deterministic netting
- both strategy intents preserved
- resulting portfolio position correct
- attribution preserved

---

# 41. UAT Phase 5 — Multiple Scenarios

Create:

```text
Scenario 1: isolated
Scenario 2: shared
Scenario 3: shared 60/40 allocation
Scenario 4: shared 50/50 allocation
```

Run simultaneously.

Expected:

- all scenarios receive unique run IDs
- no cross-contamination
- each scenario maintains independent state
- all complete independently
- experiment aggregates results

---

# 42. UAT Phase 6 — Failure Isolation

Force Scenario 2 to fail.

Expected:

```text
Scenario 1 → COMPLETED
Scenario 2 → FAILED
Scenario 3 → COMPLETED
Scenario 4 → COMPLETED
```

The failure must not corrupt completed scenarios.

---

# 43. UAT Phase 7 — Provenance

After completion, change the UI configuration.

Expected:

- old run remains unchanged
- old configuration remains immutable
- new configuration receives a new version/hash

Open an old run.

Expected:

- exact original configuration displayed
- exact strategy version displayed
- exact dataset displayed
- exact execution/cost model displayed

---

# 44. UAT Phase 8 — Ledger Reconciliation

For every completed run:

```text
sum(trade P&L)
+
fees
+
slippage
=
ledger P&L
```

Expected:

```text
difference = 0
```

within explicitly documented floating-point tolerance.

Portfolio P&L must reconcile to the sum of attributable strategy P&L plus any explicitly defined portfolio-level effects.

---

# 45. UAT Phase 9 — Backtest/Paper Consistency

Use a deterministic synthetic market fixture.

Run:

```text
Backtest
Paper simulator
```

with identical:

- strategy
- capital
- configuration
- execution assumptions

Expected:

- same signals
- same intended orders
- same fills where deterministic
- same trade sequence
- same P&L within defined execution tolerance

This validates that backtest and runtime environments do not silently implement different strategy semantics.

---

# 46. UAT Phase 10 — Live Safety

Before live execution:

Expected safeguards:

```text
Strategy version identified
Config version identified
Broker session valid
Execution mode explicitly LIVE
Capital limit valid
Risk limits valid
```

No live order may be emitted if the strategy is:

```text
BACKTEST
PAPER
SHADOW
```

---

# 47. UAT Acceptance Gate

The feature is accepted only if:

```text
All P0 UAT tests pass
All P1 UAT tests pass
No ledger reconciliation failures
No provenance failures
No cross-scenario contamination
No unexpected live orders
No database corruption
No duplicate run identities
```

---

# 48. High-Level Design — HLD

## 48.1 Architectural Layers

```text
┌────────────────────────────────────────────┐
│                  UI Layer                  │
│ React / OpenAlgo existing frontend        │
└──────────────────────┬─────────────────────┘
                       │
┌──────────────────────▼─────────────────────┐
│                  API Layer                  │
│ Flask / RESTX / existing route conventions │
└──────────────────────┬─────────────────────┘
                       │
┌──────────────────────▼─────────────────────┐
│               Service Layer                │
│ Strategy / Experiment / Backtest / Ledger │
└──────────────────────┬─────────────────────┘
                       │
             ┌─────────┴─────────┐
             ▼                   ▼
      Simulation Engine      Execution Layer
             │                   │
       Backtest Broker       OpenAlgo
             │                   │
             └─────────┬─────────┘
                       ▼
                 Persistence
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
      Research DB   Ledger DB    DuckDB
```

---

# 49. HLD — Core Components

## Strategy Registry

Owns:

- strategy identity
- strategy versions
- artifact metadata

## Configuration Manager

Owns:

- configuration versions
- configuration validation
- hashes

## Experiment Manager

Owns:

- experiments
- scenarios
- strategy instances
- execution matrix

## Backtest Engine

Owns:

- historical simulation
- simulated clock
- simulated broker
- trade generation

## Portfolio Engine

Owns:

- capital
- allocation
- positions
- portfolio constraints
- shared-mode netting
- attribution

## Execution Adapter

Owns:

- OpenAlgo integration
- order translation
- fill normalization

## Ledger

Owns:

- immutable financial events
- P&L
- reconciliation

## Analytics

Owns:

- metrics
- comparison
- attribution
- visualization

---

# 50. HLD — Event Flow

```text
Market Data
    ↓
Strategy Instance
    ↓
Signal
    ↓
Portfolio Engine
    ↓
Risk
    ↓
Desired Order
    ↓
Execution
    ↓
Fill
    ↓
Ledger
    ↓
Analytics
```

Backtesting substitutes:

```text
Historical Data
Simulated Execution
```

for:

```text
Live Data
OpenAlgo Execution
```

---

# 51. Low-Level Design — LLD Directive

## IMPORTANT

The LLD implementation must **follow the existing OpenAlgo architecture and coding conventions**.

Do not introduce a new application framework.

Do not create an independent backend.

Do not create a second frontend framework.

Do not replace Flask.

Do not replace React.

Do not replace OpenAlgo's existing broker abstractions.

Do not duplicate functionality already provided by OpenAlgo.

The existing repository code is authoritative where documentation and examples differ.

---

# 52. LLD — Backend

Follow existing:

```text
blueprints/
restx_api/
services/
database/
utils/
```

organization.

New functionality should use the same patterns.

Candidate modules:

```text
blueprints/
    experiments.py
    backtests.py
    research.py
    portfolio.py
    ledger.py

services/
    experiment_service.py
    backtest_service.py
    portfolio_service.py
    ledger_service.py
    configuration_service.py

database/
    experiment_db.py
    backtest_db.py
    research_db.py
    ledger_db.py
```

Exact placement must be determined by inspecting the current repository before implementation.

---

# 53. LLD — Frontend

Follow existing OpenAlgo React architecture.

Do not introduce another UI framework.

Reuse:

- existing layout
- navigation
- components
- state management
- API access patterns
- styling
- dialogs
- tables
- notifications

New surfaces should feel native to OpenAlgo.

---

# 54. LLD — Database

Follow existing OpenAlgo DB initialization and migration conventions.

Do not invent a separate migration framework.

Use the existing database abstractions wherever appropriate.

OpenAlgo already initializes multiple isolated database stores and uses SQLite for application-oriented stores and DuckDB for historical data.

---

# 55. LLD — Strategy Runtime

Follow the existing Python Strategy Host model.

OpenAlgo already runs strategies as isolated subprocesses and provides scheduling, logs, and parameters.

The multi-strategy runtime must build on this model rather than replacing it.

---

# 56. LLD — Backtest Runtime

Backtest jobs should have an execution boundary similar to strategy runs.

Each job receives:

```text
run_id
scenario_id
strategy_instance_ids
dataset_id
configuration
execution_model
```

The job must not directly mutate another run's state.

---

# 57. LLD — Shared Simulation

Implement:

```text
SharedPortfolio
```

with deterministic operations:

```text
collect_signals()
allocate_capital()
resolve_conflicts()
apply_risk()
net_orders()
simulate_execution()
allocate_fills()
record_ledger()
```

Every operation must be testable independently.

---

# 58. LLD — Isolated Simulation

Each isolated strategy gets:

```text
SimulatedBroker
Portfolio
Cash Account
Position Store
Ledger
```

No state is shared between worlds except immutable market-data input.

---

# 59. LLD — Shared Simulation

Shared mode gets:

```text
SharedMarketData
SharedClock
SharedCash
SharedPositions
SharedRisk
SharedExecution
```

but maintains:

```text
Strategy-level virtual books
```

for attribution.

---

# 60. LLD — Event Identity

Every event must have a globally unique identifier.

Example:

```text
experiment_id
scenario_id
strategy_instance_id
signal_id
order_id
fill_id
trade_id
ledger_entry_id
```

Never infer identity from timestamps or symbol/side combinations.

---

# 61. LLD — Idempotency

Operations must be idempotent wherever possible.

Examples:

```text
submit order
record fill
record ledger entry
complete run
```

A retry must not create duplicate financial events.

---

# 62. LLD — API

API endpoints should follow OpenAlgo's existing API naming, validation, authentication, and response conventions.

Potential surfaces:

```text
POST /api/v1/experiment/create
POST /api/v1/experiment/start
POST /api/v1/experiment/stop
POST /api/v1/experiment/list

POST /api/v1/backtest/create
POST /api/v1/backtest/start
POST /api/v1/backtest/status
POST /api/v1/backtest/results

POST /api/v1/portfolio/config
POST /api/v1/portfolio/compare

POST /api/v1/ledger/query
```

These are conceptual endpoints only.

The implementation must first inspect existing OpenAlgo routes and follow their actual conventions.

---

# 63. Observability

Every run must expose:

```text
status
start_time
end_time
duration
progress
scenario
strategy
errors
warnings
```

Logs must be associated with the run ID.

Example:

```text
run=bt_1842
scenario=sc_03
strategy=alpha_b
```

---

# 64. Security

The system must inherit OpenAlgo's existing security architecture.

Do not create parallel authentication.

Live actions require explicit authorization.

Backtest actions must never be able to invoke live broker execution.

The mode must be explicit:

```text
BACKTEST
PAPER
SHADOW
LIVE
```

---

# 65. Critical Safety Invariant

A backtest must never be capable of reaching the live broker accidentally.

There must be a hard architectural boundary:

```text
BACKTEST
    ↓
SimulatedBroker
```

versus:

```text
LIVE
    ↓
OpenAlgoExecutionAdapter
    ↓
Broker
```

No environment variable or UI checkbox alone should be the only protection.

---

# 66. Implementation Phases

## Phase 1 — Fork and Baseline

- fork OpenAlgo
- freeze upstream commit
- establish upstream tracking
- run complete existing test suite
- document baseline
- verify broker login
- verify existing Python strategy execution
- verify existing sandbox

**No product changes yet.**

---

## Phase 2 — Strategy Registry + Config

Implement:

```text
strategy versions
config versions
hashing
UI configuration
```

---

## Phase 3 — Research Run Registry

Implement:

```text
experiments
scenarios
runs
datasets
artifacts
```

---

## Phase 4 — Backtest Engine

Implement:

```text
single strategy
isolated mode
shared mode
```

---

## Phase 5 — Ledger

Implement:

```text
orders
fills
trades
positions
P&L
attribution
reconciliation
```

---

## Phase 6 — Parallel Scenario Execution

Implement:

```text
experiment matrix
parallel execution
resource limits
failure isolation
```

---

## Phase 7 — UI Analytics

Implement:

```text
run dashboard
strategy comparison
portfolio comparison
attribution
equity curves
drawdown
```

---

## Phase 8 — Paper/Shadow Integration

Connect the same strategy instances to:

```text
OpenAlgoExecutionAdapter
```

---

## Phase 9 — Live

Only after all UAT gates pass:

```text
paper
→ shadow
→ controlled live
```

---

# 67. Non-Goals

The first implementation must NOT attempt to:

- rewrite OpenAlgo's broker layer
- rewrite its WebSocket infrastructure
- replace its strategy host
- build a new broker abstraction
- build a new frontend framework
- build an ML platform
- automatically optimize strategies
- automatically select winning strategies
- automatically tune research parameters
- create an uncontrolled parameter sweep system
- support distributed cloud execution initially

---

# 68. Definition of Done

The platform is considered complete for v1 when:

### Strategy

- [ ] strategy registry works
- [ ] strategy versions immutable
- [ ] artifact hashes recorded

### Configuration

- [ ] UI configuration works
- [ ] configurations versioned
- [ ] configuration hashes recorded

### Backtesting

- [ ] single strategy works
- [ ] isolated mode works
- [ ] shared mode works
- [ ] deterministic reruns work

### Experiments

- [ ] multiple scenarios supported
- [ ] scenarios run concurrently
- [ ] failure isolation works

### Portfolio

- [ ] capital allocation works
- [ ] shared positions work
- [ ] conflicts resolved deterministically
- [ ] attribution works

### Ledger

- [ ] orders recorded
- [ ] fills recorded
- [ ] trades recorded
- [ ] P&L reconciles
- [ ] strategy attribution reconciles

### Provenance

- [ ] strategy hash
- [ ] model hash
- [ ] config hash
- [ ] dataset hash
- [ ] engine version
- [ ] run hash

### UI

- [ ] strategy registry
- [ ] configuration
- [ ] backtest creation
- [ ] experiment creation
- [ ] run monitoring
- [ ] comparison
- [ ] attribution

### Safety

- [ ] backtest cannot reach broker
- [ ] paper cannot reach broker
- [ ] shadow cannot place live orders
- [ ] live mode explicitly identified
- [ ] existing OpenAlgo safety mechanisms preserved

### UAT

- [ ] all P0 UAT passed
- [ ] all P1 UAT passed
- [ ] ledger reconciliation passed
- [ ] provenance audit passed
- [ ] cross-scenario isolation passed
- [ ] no unexpected live orders

---

# 69. Final Architectural Principle

The platform should be understood as:

```text
                     OUR PLATFORM
┌────────────────────────────────────────────────┐
│ Research                                        │
│ Experiments                                     │
│ Backtesting                                     │
│ Strategy Registry                               │
│ Configuration                                   │
│ Portfolio Simulation                            │
│ Multi-Strategy Competition                      │
│ Ledger / Attribution                            │
│ Analytics                                       │
└───────────────────────┬────────────────────────┘
                        │
                        ▼
              ┌────────────────────┐
              │     OPENALGO       │
              │                    │
              │ Broker             │
              │ Market Data        │
              │ Execution          │
              │ Strategy Host      │
              │ Scheduling         │
              │ Existing UI        │
              └─────────┬──────────┘
                        │
                        ▼
                    BROKER
```

**OpenAlgo is the execution substrate.**

**Our fork becomes the strategy research and portfolio operating system around it.**

The two multi-strategy modes are deliberately different:

```text
ISOLATED
"What happens if each strategy gets its own money?"

SHARED
"What happens if I actually deploy these strategies together?"
```

And the experiment system allows both questions to be tested across many capital/configuration combinations **using the same market environment and immutable provenance**.

That should be the architectural north star for the fork.
