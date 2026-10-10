# Multi-Strategy Algo Trading Platform
## Final Master PRD & Implementation Checklist

> **Fork note.** This PRD belongs to the `sukumar1210/openalgo` fork and is kept
> as written by its author (received 2026-10-10). It condenses
> [`research-backtest.md`](research-backtest.md) (PRD v1.0) and
> [`research-sandbox.md`](research-sandbox.md) (Phase 8) into one checklist. On
> this platform:
>
> - **Vocabulary.** *Paper*, *Paper mode* and `PAPER` are **Sandbox** everywhere:
>   in code (`environment = "SANDBOX"`), in data and on every screen (the
>   vocabulary rules in `CLAUDE.md`). Where this document says `PaperContext` or
>   `BacktestContext`, the implementation passes one `Context` in every
>   environment, because a strategy must not be able to tell them apart.
>   `SHADOW` and `LIVE` keep their names.
> - **Where it stands.** The Phase 4 gate review this document asks for (section
>   51), and the plan and status for everything after it, are in
>   [`docs/design/59-research-platform/`](../design/59-research-platform/README.md).

**Status:** Master implementation specification
**Base:** OpenAlgo fork
**Initial broker:** Zerodha Kite through OpenAlgo
**Market:** Indian equities
**Timezone:** IST
**Primary objective:** Build a versioned, auditable platform for strategy research, backtesting, multi-strategy simulation, paper trading, shadow trading, and live execution.

---

# 0. MASTER OBJECTIVE

Build a fork of OpenAlgo that extends it into a complete:

> **Strategy Research → Backtest → Competition → Portfolio Simulation → Paper → Shadow → Live platform**

while preserving OpenAlgo as the execution foundation.

The platform must support:

- strategy versioning
- configuration versioning
- strategy plugins
- backtesting
- isolated strategy competition
- shared portfolio simulation
- multiple scenarios running concurrently
- paper/analyzer mode
- shadow mode
- live execution
- virtual strategy books
- portfolio management
- order/fill/trade ledgers
- attribution
- reproducibility
- provenance
- UAT
- safety controls

---

# 1. NON-NEGOTIABLE ARCHITECTURAL PRINCIPLES

## 1.1 OpenAlgo remains the execution substrate

Do not rewrite OpenAlgo unnecessarily.

Reuse its:

- broker integrations
- market-data infrastructure
- strategy host
- scheduling
- sandbox
- authentication
- logging
- database patterns
- API conventions
- React frontend conventions

The fork adds the research/portfolio/simulation/control layer.

---

## 1.2 Strategy logic is a plugin

The platform defines a small strategy contract.

Conceptually:

```text
StrategyPlugin
    metadata()
    initialize(context)
    decide(context) -> TargetPortfolio
    on_fill(fill)
    on_session_end()
```

Strategies return **desired target positions**, not broker-specific orders.

The platform owns:

- allocation
- leverage
- risk
- position management
- order generation
- netting
- execution
- fills
- ledger
- attribution

---

## 1.3 Alpha A remains external and authoritative

Alpha A currently lives in `algo-trading` as:

- frozen artifact
- model
- feature pipeline
- daily pipeline
- research backtester

Do **not** move that research system into OpenAlgo.

Create only a thin Alpha A plugin adapter.

```text
algo-trading
    └── Alpha A
          ├── research
          ├── artifact
          ├── model
          └── adapter
                    ↓
              OpenAlgo fork
```

The adapter must preserve Alpha A's existing decision semantics.

---

## 1.4 Same strategy across environments

The same strategy version must operate in:

```text
BACKTEST
PAPER
SHADOW
LIVE
```

Only the environment changes.

```text
BACKTEST
    historical data
    simulated clock
    simulated execution

PAPER
    live data
    live clock
    sandbox execution

SHADOW
    live data
    live clock
    observational/simulated execution

LIVE
    live data
    live clock
    broker execution
```

---

# 2. STRATEGY REGISTRY

## Required

- [ ] Create strategy registry.
- [ ] Create immutable strategy versions.
- [ ] Assign unique strategy IDs.
- [ ] Store strategy version.
- [ ] Store artifact hash.
- [ ] Store model hash where applicable.
- [ ] Store package version.
- [ ] Store adapter version.
- [ ] Store creation timestamp.
- [ ] Store strategy metadata.
- [ ] Prevent mutation of completed strategy versions.

Example:

```text
Alpha A
    strategy_id = alpha-a
    version = 0.2.5
    artifact_hash = ...
    model_hash = ...
    adapter_version = ...
```

---

# 3. STRATEGY PLUGIN SYSTEM

## Required

- [ ] Define Python plugin interface.
- [ ] Define strategy discovery/registration.
- [ ] Define plugin metadata.
- [ ] Define lifecycle hooks.
- [ ] Define `decide(context)`.
- [ ] Define target-position representation.
- [ ] Define fill callback.
- [ ] Define session-end callback.
- [ ] Version plugin identity.
- [ ] Test deterministic strategy execution.

## Constraints

- [ ] Strategy cannot directly access broker.
- [ ] Strategy cannot directly access sandbox.
- [ ] Strategy cannot directly access database.
- [ ] Strategy cannot directly access market-data transport.
- [ ] Strategy operates through Context.
- [ ] Strategy returns target positions rather than orders.

---

# 4. CONFIGURATION SYSTEM

Create versioned configuration.

## Configuration fields

- [ ] capital
- [ ] allocation
- [ ] leverage
- [ ] universe
- [ ] entry schedule
- [ ] exit schedule
- [ ] stop policy
- [ ] take-profit policy
- [ ] risk limits
- [execution mode]
- [other approved deployment parameters]

Example:

```text
Alpha A
Config v12

Capital       ₹5,00,000
Allocation    100%
Leverage      1x
Entry         09:16
Exit          EOD
Execution     PAPER
```

## Requirements

- [ ] Configuration changes create new versions.
- [ ] Old configurations remain immutable.
- [ ] Configuration hash generated.
- [ ] Runs reference exact configuration version.
- [ ] UI shows configuration version.
- [ ] UI shows which runs/sessions used it.

---

# 5. RESEARCH PARAMETER SAFETY

Deployment UI must **not** become an uncontrolled model-optimization interface.

Do not expose arbitrary:

- feature selection
- model parameters
- target definitions
- training parameters
- model architecture

as casual UI knobs.

Research/model changes require a new strategy/model version.

---

# 6. DATASET SYSTEM

## Phase 4 initial provider

Use:

> **OpenAlgo `historify.duckdb`**

Do not initially couple the fork to `algo-trading`'s Parquet BarStore.

## Dataset abstraction

Create:

```text
DatasetProvider
    └── HistorifyDuckDBProvider
```

Future providers can be added later.

## Requirements

- [ ] dataset ID
- [ ] source
- [ ] symbols
- [ ] interval
- [ ] start
- [ ] end
- [ ] row count
- [ ] symbol count
- [ ] first timestamp
- [ ] last timestamp
- [ ] dataset hash
- [ ] dataset snapshot identity

## Data rules

- [ ] Strategy cannot directly access DuckDB.
- [ ] Missing data is explicit.
- [ ] No silent zero-filling.
- [ ] Timestamp conventions are consistent.
- [ ] Existing OpenAlgo historical-data infrastructure is reused.
- [ ] Canonical dataset fingerprint is deterministic.

---

# 7. EXPERIMENT REGISTRY

Create an experiment abstraction.

```text
Experiment
    └── Scenario
          ├── Strategy Instance
          ├── Environment
          ├── Capital
          ├── Universe
          ├── Dataset
          ├── Cost Model
          └── Execution Model
```

## Required

- [ ] experiment ID
- [ ] name
- [ ] description
- [ ] objective
- [ ] scenarios
- [ ] status
- [ ] timestamps
- [ ] result aggregation

---

# 8. SCENARIO SYSTEM

Each scenario is a complete execution environment.

Store:

- [ ] scenario ID
- [ ] interaction mode
- [ ] environment
- [ ] dataset
- [ ] universe
- [ ] capital policy
- [ ] allocation
- [ ] leverage
- [ ] cost model
- [ ] execution model
- [ ] strategy instances

---

# 9. MULTI-STRATEGY MODE A — ISOLATED

## Definition

Each strategy gets an independent simulated world.

They share:

- [ ] market data
- [ ] dataset
- [ ] universe
- [clock
- [ ] execution assumptions

They do not share:

- [ ] capital
- [ ] positions
- [ ] buying power
- [ ] risk
- [ ] portfolio constraints

Example:

```text
Alpha A → ₹5L → Portfolio A

Alpha B → ₹5L → Portfolio B
```

A's RELIANCE position must not affect B.

## Tests

- [ ] independent cash
- [ ] independent positions
- [ ] independent P&L
- [ ] independent risk state
- [ ] no state leakage

---

# 10. MULTI-STRATEGY MODE B — SHARED

## Definition

All strategies operate in one simulated portfolio.

They share:

- [ ] capital
- [ ] buying power
- [ ] positions
- [ ] portfolio risk
- [ ] exposure
- [ ] execution environment

They retain:

- [ ] strategy identity
- [ ] strategy intent
- [ ] virtual strategy books
- [ ] strategy attribution

Example:

```text
Alpha A ──┐
          ├── Shared Portfolio
Alpha B ──┘
```

---

# 11. SHARED-MODE CONFLICT RESOLUTION

Implement deterministic pipeline:

```text
Signals
   ↓
Target Positions
   ↓
Portfolio Allocation
   ↓
Conflict Resolution
   ↓
Risk Checks
   ↓
Net Desired Positions
   ↓
Orders
   ↓
Execution
   ↓
Fills
   ↓
Attribution
```

Must define behavior for:

- [ ] same-direction signals
- [ ] opposing signals
- [ ] capital shortage
- [ ] leverage limits
- [ ] exposure limits
- [ ] risk rejection
- [ ] partial execution

---

# 12. MULTIPLE SCENARIOS IN PARALLEL

Support:

```text
Scenario 1
    A/B isolated

Scenario 2
    A/B shared

Scenario 3
    A 60% / B 40% shared

Scenario 4
    A 50% / B 50% shared
```

## Requirements

- [ ] unique scenario IDs
- [ ] unique run IDs
- [ ] isolated mutable state
- [ ] common immutable market data where appropriate
- [ ] concurrent execution
- [ ] resource limits
- [ ] independent failure handling
- [ ] no cross-scenario contamination

---

# 13. BACKTEST ENGINE

## Single strategy

Support:

- [ ] strategy selection
- [ ] version selection
- [ ] config selection
- [ ] dataset selection
- [ ] universe
- [ ] date range
- [ ] capital
- [ ] costs
- [ ] slippage
- [ ] execution model

## Modes

- [ ] single
- [ ] isolated
- [ ] shared

---

# 14. BACKTEST DETERMINISM

For identical:

```text
strategy version
configuration
dataset
universe
cost model
execution model
interaction mode
engine version
```

the result must be reproducible.

Store:

- [ ] run hash
- [ ] random seeds
- [ ] engine version
- [ ] dataset hash
- [ ] config hash
- [ ] strategy hash

---

# 15. BACKTEST RESULTS

Store:

- [ ] run ID
- [ ] status
- [ ] duration
- [ ] capital
- [ ] final capital
- [ ] gross P&L
- [ ] net P&L
- [ ] return
- [ ] drawdown
- [ ] Sharpe
- [ ] Sortino
- [ ] win rate
- [ ] profit factor
- [ ] trade count
- [ ] turnover
- [ ] costs
- [ ] exposure
- [ ] artifacts

---

# 16. MINIMAL LEDGER

The ledger must be shared by Backtest, Paper, Shadow, and Live.

Events:

- [ ] SIGNAL
- [ ] TARGET_POSITION
- [ ] ORDER_INTENT
- [ ] ORDER_SUBMITTED
- [ ] ORDER_REJECTED
- [ ] FILL
- [ ] POSITION_UPDATE
- [ ] FEE
- [ ] SLIPPAGE
- [ ] REALIZED_PNL
- [ ] UNREALIZED_PNL
- [ ] SESSION_EVENT
- [ ] SQUARE_OFF

Every event should carry, where applicable:

```text
strategy_id
strategy_version_id
config_version_id
experiment_id
scenario_id
session_id
strategy_instance_id
signal_id
platform_order_id
execution_order_id
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

---

# 17. ORDER IDENTITY

Platform identity is authoritative.

```text
platform_order_id
       ↓
OpenAlgo order_id
       ↓
fill(s)
```

OpenAlgo order ID is an execution reference.

It is not the primary research identity.

---

# 18. LEDGER RECONCILIATION

Must reconcile:

```text
Trade P&L
+ fees
+ slippage
= ledger P&L
```

and:

```text
Strategy contributions
→ portfolio result
```

within defined numerical tolerance.

---

# 19. ARTIFACT STORAGE

Database stores metadata.

Files/object storage stores large artifacts.

Examples:

- [ ] feature panels
- [ ] score panels
- [ ] equity curves
- [ ] diagnostics
- [ ] model artifacts
- [ ] Parquet files

DB records:

```text
artifact_uri
artifact_hash
artifact_type
artifact_size
```

The complete per-symbol score/prediction panel must remain persistently auditable where required by research policy.

---

# 20. PAPER / ANALYZER MODE

Paper is **Phase 8**, but is part of the same architecture.

Paper means:

> Live market data + live clock + simulated execution.

It is not a separate strategy implementation.

---

# 21. PAPER — ISOLATED

Support:

```text
Alpha A → virtual ₹5L → Portfolio A
Alpha B → virtual ₹5L → Portfolio B
```

They share live market data but not virtual capital/positions.

The sandbox account may be shared underneath.

The platform's virtual books are authoritative.

---

# 22. PAPER — SHARED

Support:

```text
Alpha A ──┐
          ├── Shared Virtual Portfolio
Alpha B ──┘
```

Use the same shared portfolio engine as Backtest.

---

# 23. PAPER — MULTIPLE CONCURRENT SCENARIOS

Support simultaneous:

```text
Paper Scenario 1
    isolated

Paper Scenario 2
    shared

Paper Scenario 3
    shared 60/40
```

Each has independent:

- [ ] session
- [ ] scenario
- [ ] virtual portfolio
- [ ] ledger
- [ ] strategy instances

---

# 24. PAPER — SANDBOX

OpenAlgo sandbox is used only for:

- [ ] simulated order acceptance
- [ ] simulated fills
- [ ] execution status

It is not authoritative for:

- [ ] strategy capital
- [ ] strategy positions
- [ ] allocation
- [ ] portfolio P&L
- [ ] strategy attribution

Therefore:

> Sandbox Position ≠ Platform Virtual Position.

---

# 25. PAPER — SAFETY

This is a P0 requirement.

Paper must never reach the live broker.

Required architecture:

```text
PAPER
  ↓
SandboxExecutionAdapter
  ↓
OpenAlgo Sandbox
```

Live:

```text
LIVE
  ↓
OpenAlgoExecutionAdapter
  ↓
Broker
```

Do not rely on the global Analyzer/Live switch for safety.

Changing the global switch must not turn Paper into Live.

---

# 26. PAPER — LIVE DATA

Reuse OpenAlgo's existing live market-data infrastructure.

Do not create a second market-data transport without a demonstrated requirement.

The strategy receives:

```text
PaperContext
```

not direct WebSocket/REST access.

---

# 27. PAPER — SESSION

Initial definition:

> One trading day = one Paper session.

States:

```text
CREATED
STARTING
RUNNING
PAUSED
STOPPING
COMPLETED
FAILED
CANCELLED
```

Handle:

- [ ] startup
- [ ] market open
- [ ] data disconnect
- [ ] sandbox disconnect
- [ ] strategy crash
- [ ] platform restart
- [ ] order rejection
- [ ] EOD
- [ ] square-off
- [ ] recovery

---

# 28. PAPER — RESTART / RECOVERY

On restart:

- [ ] detect incomplete sessions
- [ ] restore virtual portfolio
- [ ] restore positions
- [ ] restore orders
- [ ] reconcile sandbox state
- [ ] prevent duplicate orders
- [ ] continue or safely terminate according to policy

---

# 29. PAPER — BACKTEST PARITY

For deterministic fixtures:

```text
same strategy
same version
same config
same market sequence
same capital
same execution assumptions
```

Backtest and Paper must produce equivalent:

- [ ] signals
- [ ] target positions
- [ ] order intents
- [ ] fills
- [ ] P&L

within explicitly defined execution tolerances.

---

# 30. SHADOW MODE

Phase 9 or later.

Must reuse the same:

- [ ] strategy
- [ ] config
- [ ] portfolio
- [ ] ledger
- [ ] attribution

architecture.

Shadow must never send live orders.

---

# 31. LIVE MODE

Phase 9 or later.

Must reuse the same:

- [ ] strategy identity
- [ ] configuration
- [ ] portfolio
- [ ] ledger
- [ ] attribution

but use:

```text
OpenAlgoExecutionAdapter
    ↓
Broker
```

Live execution requires explicit:

- [ ] LIVE environment
- [ ] valid broker session
- [ ] capital limits
- [ ] risk checks
- [ ] strategy enablement

---

# 32. UI — STRATEGY REGISTRY

Display:

- [ ] strategy
- [ ] version
- [ ] status
- [ ] configuration
- [ ] last run
- [ ] P&L

Actions:

- [ ] Backtest
- [ ] Paper
- [ ] Shadow
- [ ] Live
- [ ] Pause
- [ ] Stop

---

# 33. UI — CONFIGURATION

Allow:

- [ ] capital
- [ ] allocation
- [ ] leverage
- [ ] universe
- [ ] schedule
- [ ] risk
- [ ] execution environment

Display:

- [ ] config version
- [ ] config hash
- [ ] creation time
- [ ] usage history

---

# 34. UI — EXPERIMENT BUILDER

Allow:

- [ ] create experiment
- [ ] add strategy
- [ ] choose strategy version
- [ ] choose config
- [ ] choose dataset
- [ ] choose interaction mode
- [ ] add multiple scenarios
- [ ] run scenarios concurrently

---

# 35. UI — BACKTEST

Provide:

- [ ] strategy selection
- [ ] dataset
- [ ] universe
- [ ] date range
- [ ] capital
- [ ] cost model
- [ ] slippage
- [ ] mode
- [ ] run button

---

# 36. UI — PAPER

Display:

- [ ] PAPER indicator
- [ ] session ID
- [ ] scenario
- [ ] market-data status
- [ ] sandbox status
- [ ] capital
- [ ] P&L
- [ ] positions
- [ ] orders
- [ ] fills
- [ ] last signal

---

# 37. UI — ANALYTICS

Support:

- [ ] strategy comparison
- [ ] scenario comparison
- [ ] isolated vs shared comparison
- [ ] configuration comparison
- [ ] equity curves
- [ ] drawdown
- [ ] exposure
- [ ] costs
- [ ] attribution

Shared-mode analytics additionally:

- [ ] overlap
- [ ] conflict count
- [ ] capital competition
- [ ] netting impact
- [ ] strategy contribution

---

# 38. HIGH-LEVEL ARCHITECTURE

```text
                         WEB UI
                           │
                           ▼
                       API Layer
                           │
                           ▼
                     Service Layer
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
      Strategy          Experiment       Portfolio
      Registry          Manager           Engine
          │                │                │
          └────────────────┼────────────────┘
                           ▼
                    Simulation Engine
                           │
                ┌──────────┴──────────┐
                ▼                     ▼
             Backtest              Runtime
                │                     │
                │             ┌───────┼────────┐
                │             ▼       ▼        ▼
                │           Paper  Shadow    Live
                │             │       │        │
                └─────────────┴───────┴────────┘
                              │
                            Ledger
                              │
                              ▼
                         Persistence
```

---

# 39. OPENALGO LLD DIRECTIVE

The LLD must follow the **existing OpenAlgo implementation**.

Before implementing:

- [ ] inspect repository architecture
- [ ] inspect existing blueprints
- [ ] inspect REST/API patterns
- [ ] inspect service patterns
- [ ] inspect DB abstractions
- [ ] inspect strategy host
- [ ] inspect sandbox
- [ ] inspect scheduler
- [ ] inspect market data
- [ ] inspect React frontend
- [ ] inspect existing tests

Do not invent a new framework.

Do not introduce:

- [ ] new backend framework
- [ ] new frontend framework
- [ ] second broker abstraction
- [ ] second authentication system
- [ ] second sandbox
- [ ] second market-data system
- [ ] second portfolio engine
- [ ] second ledger

Where OpenAlgo already provides the required capability, extend it.

---

# 40. DATABASE ARCHITECTURE

Follow OpenAlgo's existing database conventions.

Conceptual entities:

```text
strategies
strategy_versions
strategy_configs

experiments
scenarios
strategy_instances

datasets
dataset_snapshots

backtest_runs
paper_sessions
live_sessions

orders
fills
positions
trades
ledger_entries
```

Do not create duplicate tables when an existing OpenAlgo entity can naturally support the requirement.

---

# 41. PROVENANCE

Every result/session must identify:

```text
strategy_id
strategy_version_id
artifact_hash
model_hash
adapter_version

config_version_id
config_hash

dataset_id
dataset_hash

experiment_id
scenario_id
interaction_mode

execution_environment
execution_model
cost_model

engine_version
run_hash
```

A result without sufficient provenance is not considered a valid research result.

---

# 42. LOGGING

Every important operation must be searchable using:

```text
run_id
experiment_id
scenario_id
session_id
strategy_instance_id
platform_order_id
```

Example:

```text
session=paper-20261010-001
scenario=sc-03
strategy=alpha-a
order=po-123
event=FILL
```

---

# 43. IDEMPOTENCY

The following must be idempotent:

- [ ] run creation
- [ ] order creation
- [ ] fill processing
- [ ] ledger creation
- [ ] position update
- [ ] session completion
- [ ] EOD square-off

Retries must never create duplicate financial events.

---

# 44. RESOURCE MANAGEMENT

Parallel runs must have configurable limits:

- [ ] max concurrent backtests
- [ ] max concurrent scenarios
- [ ] max memory
- [ ] max CPU
- [ ] job timeout

Run states:

```text
QUEUED
RUNNING
COMPLETED
FAILED
CANCELLED
```

---

# 45. FAILURE ISOLATION

If:

```text
Scenario 2 → FAILED
```

then:

```text
Scenario 1 → unaffected
Scenario 3 → unaffected
Scenario 4 → unaffected
```

No scenario may corrupt another scenario's state.

---

# 46. SECURITY

Reuse OpenAlgo authentication and authorization.

Requirements:

- [ ] no new auth system
- [ ] explicit LIVE mode
- [ ] Paper safety boundary
- [ ] Backtest safety boundary
- [ ] Shadow safety boundary
- [ ] live action authorization
- [ ] sensitive broker credentials remain outside strategy plugins

---

# 47. PHASE 1 — FORK & BASELINE

## Deliverables

- [ ] fork OpenAlgo
- [ ] pin baseline commit
- [ ] establish upstream tracking
- [ ] document baseline
- [ ] run complete existing test suite
- [ ] verify current broker integration
- [ ] verify strategy host
- [ ] verify sandbox
- [ ] verify market data
- [ ] verify frontend

## Gate

No product changes until baseline passes.

---

# 48. PHASE 2 — STRATEGY REGISTRY + CONFIG

## Deliverables

- [ ] strategy registry
- [ ] strategy versions
- [ ] artifact hashes
- [ ] config versions
- [ ] config hashes
- [ ] UI registry
- [ ] UI configuration
- [ ] validation
- [ ] tests
- [ ] UAT

---

# 49. PHASE 3 — EXPERIMENT / RUN REGISTRY

## Deliverables

- [ ] experiments
- [ ] scenarios
- [ ] strategy instances
- [ ] datasets
- [ ] dataset snapshots
- [ ] run registry
- [ ] run hashes
- [ ] artifact references
- [ ] run status
- [ ] provenance
- [ ] UI

---

# 50. PHASE 4 — BACKTEST ENGINE

## Deliverables

### Strategy

- [ ] plugin contract
- [ ] Alpha A adapter
- [ ] plugin discovery
- [ ] deterministic execution

### Data

- [ ] DatasetProvider
- [ ] HistorifyDuckDBProvider
- [ ] dataset snapshots
- [ ] dataset hashing

### Simulation

- [ ] BacktestContext
- [ ] simulated clock
- [ ] simulated broker
- [ ] single-strategy mode
- [ ] isolated mode
- [ ] shared mode

### Portfolio

- [ ] capital
- [ ] allocation
- [ ] leverage
- [ ] positions
- [ ] risk
- [ ] conflict resolution
- [ ] attribution

### Ledger

- [ ] orders
- [ ] fills
- [ ] trades
- [ ] positions
- [ ] P&L

### Parallel execution

- [ ] scenario scheduler
- [ ] concurrency
- [ ] resource limits
- [ ] failure isolation

---

# 51. PHASE 4 ARCHITECTURAL GATE

Do not proceed directly to later implementation.

Review:

- [ ] plugin abstraction
- [ ] Alpha A parity
- [ ] data provider
- [ ] isolated simulation
- [ ] shared simulation
- [ ] portfolio semantics
- [ ] conflict resolution
- [ ] ledger reconciliation
- [ ] deterministic reruns
- [ ] performance
- [ ] UAT

Document:

- [ ] assumptions validated
- [ ] assumptions invalidated
- [ ] limitations
- [ ] required architectural changes

Only then finalize detailed plans for later phases.

---

# 52. PHASE 5 — FULL LEDGER

High-level only until Phase 4 review.

- [ ] production-grade ledger
- [ ] complete order lifecycle
- [ ] fill lifecycle
- [ ] trade reconstruction
- [ ] reconciliation
- [ ] attribution
- [ ] cost accounting
- [ ] audit tools

---

# 53. PHASE 6 — PORTFOLIO / ANALYTICS

High-level.

- [ ] portfolio analytics
- [ ] strategy attribution
- [ ] risk
- [ ] exposure
- [ ] capital utilization
- [ ] strategy comparison
- [ ] shared portfolio analytics
- [ ] dashboards

---

# 54. PHASE 7 — PRODUCTION HARDENING

High-level.

- [ ] observability
- [ ] resource management
- [ ] recovery
- [ ] operational controls
- [ ] alerting
- [ ] auditability
- [ ] performance
- [ ] security hardening

---

# 55. PHASE 8 — PAPER / ANALYZER

High-level until Phase 4 architecture is validated.

- [ ] PaperContext
- [ ] live market data
- [ ] sandbox adapter
- [ ] platform order correlation
- [ ] isolated Paper
- [ ] shared Paper
- [ ] concurrent Paper scenarios
- [ ] virtual capital
- [ ] Paper ledger
- [ ] session lifecycle
- [ ] EOD
- [ ] restart/recovery
- [ ] Paper UI
- [ ] backtest/Paper parity
- [ ] hard broker safety

---

# 56. PHASE 9 — SHADOW + LIVE

High-level.

## Shadow

- [ ] live data
- [ ] live strategy
- [ ] no live orders
- [ ] simulated/observational execution
- [ ] shadow ledger
- [ ] comparison with Paper

## Live

- [ ] OpenAlgo execution adapter
- [ ] broker execution
- [ ] explicit LIVE environment
- [ ] risk controls
- [ ] capital limits
- [ ] kill switch
- [ ] broker reconciliation
- [ ] production ledger
- [ ] operational monitoring

---

# 57. UAT MASTER CHECKLIST

## Configuration

- [ ] strategy registration
- [ ] strategy versioning
- [ ] configuration creation
- [ ] configuration versioning
- [ ] configuration immutability

## Backtest

- [ ] single strategy
- [ ] deterministic rerun
- [ ] isolated A/B
- [ ] shared A/B
- [ ] opposing signals
- [ ] capital constraints
- [ ] attribution

## Parallel execution

- [ ] multiple scenarios
- [ ] concurrent runs
- [ ] state isolation
- [ ] failure isolation

## Ledger

- [ ] order reconciliation
- [ ] fill reconciliation
- [ ] trade reconciliation
- [ ] P&L reconciliation
- [ ] strategy attribution

## Paper

- [ ] live data
- [ ] sandbox fills
- [ ] isolated
- [ ] shared
- [ ] concurrent scenarios
- [ ] restart
- [ ] EOD
- [ ] rejection
- [ ] no broker access

## Safety

- [ ] Backtest cannot reach broker
- [ ] Paper cannot reach broker
- [ ] Shadow cannot reach broker
- [ ] global toggle cannot bypass safety
- [ ] Live requires explicit mode

---

# 58. CRITICAL SAFETY TESTS

These are mandatory automated tests.

### Test 1

```text
BACKTEST → broker call
```

Expected:

```text
IMPOSSIBLE / REJECTED
```

### Test 2

```text
PAPER → broker call
```

Expected:

```text
IMPOSSIBLE / REJECTED
```

### Test 3

```text
SHADOW → broker call
```

Expected:

```text
IMPOSSIBLE / REJECTED
```

### Test 4

Change global Analyzer/Live switch during Paper.

Expected:

```text
Paper remains Paper.
```

### Test 5

Restart Paper session.

Expected:

```text
No duplicate orders.
No duplicate fills.
No duplicate ledger entries.
```

---

# 59. UAT — BACKTEST/PAPER PARITY

Use deterministic synthetic market fixtures.

Run:

```text
same strategy
same config
same capital
same market sequence
```

through:

```text
Backtest
Paper
```

Compare:

- [ ] signals
- [ ] target positions
- [ ] orders
- [ ] fills
- [ ] positions
- [ ] P&L

Any difference must be explainable by the documented environment difference.

---

# 60. UAT — MULTI-STRATEGY COMPETITION

Test:

```text
A = ₹5L
B = ₹5L
same universe
same data
same costs
```

in:

```text
ISOLATED
SHARED
```

Verify that the results answer two different questions:

### Isolated

> Which strategy performs better independently?

### Shared

> What happens when the strategies are deployed together?

---

# 61. UAT — CONFIGURATION MATRIX

Test:

```text
A 50/50 B
A 60/40 B
A 70/30 B

isolated
shared
```

Run all combinations simultaneously.

Verify:

- [ ] each scenario has unique identity
- [ ] each configuration is immutable
- [ ] no state leakage
- [ ] results are separately attributable

---

# 62. PROVENANCE AUDIT

For any historical run, an operator must be able to answer:

```text
What strategy ran?

Which exact version?

Which model?

Which artifact?

Which configuration?

Which dataset?

Which universe?

Which costs?

Which execution model?

Which interaction mode?

Which engine version?

Which code/artifact hash?

What trades happened?

What fills happened?

How was P&L calculated?
```

If these cannot be reconstructed, the run fails the provenance requirement.

---

# 63. IMPLEMENTATION DISCIPLINE

For every phase:

- [ ] inspect existing OpenAlgo implementation first
- [ ] identify reusable components
- [ ] define interfaces
- [ ] define schema
- [ ] write tests
- [ ] implement
- [ ] run integration tests
- [ ] run UAT
- [ ] perform provenance audit
- [ ] commit
- [ ] document result

Do not allow implementation to silently redefine product requirements.

---

# 64. COMMIT / PROVENANCE DISCIPLINE

Important architectural changes should be separated into meaningful commits.

Prefer:

```text
registration
    ↓
schema/interface
    ↓
implementation
    ↓
tests
    ↓
UAT/result
```

Do not combine:

```text
research registration
implementation
result
```

into one indistinguishable commit when the work is intended to be an auditable experiment.

---

# 65. FINAL DEFINITION OF DONE

The platform is ultimately complete when this lifecycle works:

```text
                STRATEGY REGISTRY
                       │
                       ▼
                 VERSIONED MODEL
                       │
                       ▼
                 VERSIONED CONFIG
                       │
                       ▼
                  EXPERIMENT
                       │
              ┌────────┴────────┐
              ▼                 ▼
          ISOLATED           SHARED
              │                 │
              └────────┬────────┘
                       ▼
                    BACKTEST
                       │
                       ▼
                    PAPER
                       │
                       ▼
                    SHADOW
                       │
                       ▼
                     LIVE
                       │
                       ▼
                    LEDGER
                       │
                       ▼
                  ANALYTICS
```

And at every stage:

```text
Same Strategy Identity
Same Version
Same Configuration Identity
Same Portfolio Semantics
Same Ledger Semantics
Complete Provenance
```

with only the environment-specific components changing.

---

# 66. FINAL CHECKLIST

## Foundation

- [ ] OpenAlgo fork created
- [ ] upstream baseline frozen
- [ ] existing test suite passing
- [ ] architecture documented

## Strategy

- [ ] registry
- [ ] versioning
- [ ] plugin contract
- [ ] artifact hashes
- [ ] Alpha A adapter
- [ ] decision parity

## Configuration

- [ ] UI
- [ ] versioning
- [ ] hashes
- [ ] immutable history

## Data

- [ ] DatasetProvider
- [ ] Historify provider
- [ ] dataset snapshots
- [ ] dataset hashes
- [ ] missing-data policy

## Research

- [ ] experiments
- [ ] scenarios
- [ ] run registry
- [ ] provenance
- [ ] artifacts

## Backtest

- [ ] single
- [ ] isolated
- [ ] shared
- [ ] deterministic
- [ ] parallel scenarios

## Portfolio

- [ ] capital
- [ ] allocation
- [ ] leverage
- [ ] positions
- [ ] risk
- [ ] conflict resolution
- [ ] attribution

## Ledger

- [ ] orders
- [ ] fills
- [ ] trades
- [ ] positions
- [ ] P&L
- [ ] reconciliation

## Paper

- [ ] live data
- [ ] sandbox
- [ ] isolated
- [ ] shared
- [ ] multiple sessions
- [ ] virtual capital
- [ ] session lifecycle
- [ ] restart recovery
- [ ] EOD
- [ ] Paper UI

## Shadow

- [ ] live data
- [ ] no live orders
- [ ] shadow ledger
- [ ] comparison

## Live

- [ ] broker adapter
- [ ] risk
- [ ] capital limits
- [ ] kill switch
- [ ] reconciliation
- [ ] monitoring

## Safety

- [ ] Backtest cannot reach broker
- [ ] Paper cannot reach broker
- [ ] Shadow cannot reach broker
- [ ] global switch cannot bypass safety
- [ ] Live explicitly enabled

## UAT

- [ ] configuration UAT
- [ ] backtest UAT
- [ ] isolated UAT
- [ ] shared UAT
- [ ] concurrent scenario UAT
- [ ] ledger reconciliation
- [ ] provenance audit
- [ ] Paper UAT
- [ ] safety UAT
- [ ] restart/recovery UAT

## Architecture

- [ ] OpenAlgo conventions followed
- [ ] no unnecessary framework changes
- [ ] no duplicated broker system
- [ ] no duplicated portfolio system
- [ ] no duplicated ledger
- [ ] no duplicated market-data system
- [ ] no strategy-specific execution logic in platform
- [ ] no platform-specific broker logic in strategy

---

# 67. NORTH-STAR ARCHITECTURE

The final system should be:

```text
                         ┌───────────────────┐
                         │       UI          │
                         │ Config / Runs /   │
                         │ Portfolio / UAT   │
                         └─────────┬─────────┘
                                   │
                         ┌─────────▼─────────┐
                         │   Platform Core   │
                         │                   │
                         │ Strategy Registry │
                         │ Config Registry   │
                         │ Experiments       │
                         │ Portfolio         │
                         │ Ledger            │
                         │ Analytics         │
                         └─────────┬─────────┘
                                   │
                         ┌─────────▼─────────┐
                         │ Strategy Plugins  │
                         │                   │
                         │ Alpha A           │
                         │ Alpha B           │
                         │ Alpha C           │
                         └─────────┬─────────┘
                                   │
             ┌─────────────────────┼─────────────────────┐
             │                     │                     │
             ▼                     ▼                     ▼
         BACKTEST                PAPER                LIVE
             │                     │                     │
      Historical Data          Live Data             Live Data
             │                     │                     │
      Simulated Exec           Sandbox               OpenAlgo
             │                     │                     │
             └─────────────────────┼─────────────────────┘
                                   │
                              Common Ledger
                                   │
                                   ▼
                              Persistence
```

**The fundamental rule is:**

> **Strategies decide what they want. The platform decides how that desired portfolio becomes an executable portfolio. OpenAlgo remains responsible for execution infrastructure; the platform remains responsible for strategy identity, simulation, portfolio semantics, provenance, and attribution.**

That is the architecture the entire nine-phase implementation should converge toward.
