# Phase 8 — Paper / Analyzer Multi-Strategy Trading
## Product Requirements Document

> **Fork note.** This PRD belongs to the `sukumar1210/openalgo` fork and is kept
> as written by its author, as Phase 8 of
> [`research-backtest.md`](research-backtest.md). On this platform:
>
> - **Vocabulary.** *Paper*, *Paper mode* and `PAPER` are **Sandbox** everywhere:
>   in code (`environment = "SANDBOX"`), in data, and on every screen. The repo's
>   vocabulary rules in `CLAUDE.md` forbid "paper" anywhere a trader reads it,
>   and a third word for the same idea in the code is how the two drift. Where
>   this document says `PaperContext`, the implementation's name is
>   `SandboxContext`. `SHADOW` and `LIVE` keep their names.
> - **Decisions taken with the author before implementation** (2026-10-07): daily
>   sessions follow the Python Strategy Host's schedule model (start and stop
>   times on chosen days, IST, skipped on exchange holidays); live bars are built
>   from WebSocket ticks; an interrupted session resumes, or squares off when it
>   cannot. The design and what is built are in
>   [`docs/design/58-research-sandbox/README.md`](../design/58-research-sandbox/README.md).

**Status:** Planned — implementation deferred
**Parent system:** OpenAlgo fork + Multi-Strategy Platform
**Phase:** 8
**Primary objective:** Connect the existing versioned strategy, portfolio, configuration, and ledger architecture to live market data with simulated execution through OpenAlgo's sandbox.

---

# 1. Purpose

Phase 8 introduces **Paper / Analyzer mode**.

Paper mode allows production-style strategy instances to operate against **live market data** while orders are executed through a **simulated sandbox**, never through the live broker.

Paper mode must use the same fundamental objects established by the backtest engine:

- Strategy
- Strategy Version
- Strategy Instance
- Configuration
- Scenario
- Portfolio
- Position
- Order
- Fill
- Ledger
- Attribution

The environment-specific components change:

```text
BACKTEST
    Historical Data
    Simulated Clock
    Simulated Execution

PAPER
    Live Market Data
    Live Clock
    Sandbox Execution

SHADOW
    Live Market Data
    Observational / simulated execution

LIVE
    Live Market Data
    Real Broker Execution
```

The strategy itself should not need to know which environment it is running in.

---

# 2. Product Principle

## Paper is not a separate trading system.

Paper is the **same strategy and portfolio runtime operating against live market data with a hard simulated-execution boundary**.

The architecture must therefore be:

```text
                         STRATEGY
                            │
                     Target Positions
                            │
                            ▼
                       PORTFOLIO
                            │
                     Desired Orders
                            │
                ┌───────────┴───────────┐
                │                       │
             PAPER                    LIVE
                │                       │
       Sandbox Adapter          OpenAlgo Adapter
                │                       │
           OpenAlgo              Broker
           Sandbox
```

The strategy, portfolio, risk, ledger, and attribution layers must not be duplicated between Paper and Live.

---

# 3. Goals

Phase 8 must provide:

1. Paper execution against live market data.
2. Hard separation between Paper and Live execution.
3. Isolated multi-strategy Paper mode.
4. Shared multi-strategy Paper mode.
5. Multiple concurrent Paper scenarios.
6. Per-strategy virtual capital.
7. Shared virtual portfolio capital.
8. Strategy-level positions and ledgers.
9. Sandbox order/fill correlation.
10. Session lifecycle management.
11. Restart/recovery.
12. EOD session handling.
13. Paper/backtest strategy parity.
14. Complete provenance.
15. Paper analytics through the existing UI.

---

# 4. Non-Goals

Phase 8 does not introduce:

- new strategy research
- model training
- strategy optimization
- automatic strategy selection
- new broker integrations
- a replacement for OpenAlgo's sandbox
- a replacement for OpenAlgo's market-data system
- a second authentication system
- a new frontend framework
- a new portfolio system separate from the backtest portfolio engine

---

# 5. User Stories

## US-001 — Start a Paper Strategy

As an operator, I want to select a registered strategy and start it in Paper mode so that I can observe its behavior using live market data without risking capital.

---

## US-002 — Configure Paper Capital

As an operator, I want to assign virtual capital and leverage to a Paper strategy.

Example:

```text
Alpha A
Capital: ₹5,00,000
Leverage: 1x
```

---

## US-003 — Run Two Strategies Independently

As an operator, I want Alpha A and Alpha B to run independently with the same market environment and independent virtual books.

---

## US-004 — Run Two Strategies Together

As an operator, I want Alpha A and Alpha B to share one virtual portfolio so that I can see how they behave when deployed together.

---

## US-005 — Run Multiple Scenarios

As an operator, I want several Paper scenarios to run simultaneously.

Example:

```text
Scenario A
  Isolated
  Alpha A ₹5L
  Alpha B ₹5L

Scenario B
  Shared
  Alpha A ₹5L
  Alpha B ₹5L

Scenario C
  Shared
  Alpha A ₹3L
  Alpha B ₹2L
```

---

## US-006 — Audit a Paper Trade

As an operator, I want to trace every Paper fill back to:

```text
Strategy
Strategy Version
Configuration
Scenario
Session
Signal
Order
Fill
```

---

# 6. Paper Modes

## 6.1 Isolated Paper

Each strategy receives its own virtual environment.

```text
                   Live Market Data
                         │
              ┌──────────┴──────────┐
              │                     │
           Alpha A               Alpha B
              │                     │
            ₹5L                   ₹5L
              │                     │
         Portfolio A          Portfolio B
              │                     │
         Ledger A             Ledger B
```

They share market data but not:

- capital
- positions
- buying power
- risk state
- portfolio constraints

A position taken by Alpha A does not affect Alpha B.

---

# 7. Shared Paper

All strategies participate in one virtual portfolio.

```text
                  Live Market Data
                         │
             ┌───────────┴───────────┐
             │                       │
          Alpha A                 Alpha B
             │                       │
             └───────────┬───────────┘
                         ▼
                 Shared Portfolio
                         │
                   Common Capital
                         │
                  Common Positions
                         │
                    Net Orders
                         │
                     Sandbox
```

The platform must preserve strategy-level virtual books even though the portfolio is shared.

---

# 8. Virtual Capital

The OpenAlgo sandbox account is **not the source of truth for strategy capital**.

The platform owns virtual capital.

Example:

```text
Sandbox:
₹1 crore simulated account

Platform:

Alpha A → ₹2.5L
Alpha B → ₹2.5L
```

The sandbox is only used for:

- accepting simulated orders
- generating simulated fills
- reporting simulated execution status

The platform owns:

- strategy capital
- allocation
- positions
- exposure
- P&L
- attribution

Therefore:

> Sandbox Position ≠ Strategy Position.

---

# 9. Concurrent Paper Scenarios

The system must allow multiple scenarios to run simultaneously.

Each scenario must have independent:

- session state
- virtual portfolio
- positions
- risk state
- ledger
- strategy instances

They may use the same sandbox account.

Example:

```text
Session 101
  Isolated
  Alpha A
  Alpha B

Session 102
  Shared
  Alpha A
  Alpha B

Session 103
  Shared
  Alpha A 60%
  Alpha B 40%
```

No scenario may read or mutate another scenario's mutable portfolio state.

---

# 10. Strategy Identity

Paper must preserve the same strategy identity used by Backtest.

Every strategy instance must reference:

```text
strategy_id
strategy_version_id
config_version_id
artifact_hash
model_hash
adapter_version
scenario_id
session_id
```

The strategy must not become a new identity merely because it entered Paper mode.

---

# 11. Configuration

Paper uses the same versioned configuration system.

Example:

```text
Strategy:
Alpha A

Strategy Version:
0.2.5

Config:
v12

Capital:
₹5,00,000

Allocation:
100%

Leverage:
1x

Entry:
09:16

Exit:
EOD

Execution:
PAPER
```

Configuration changes create new versions.

Completed sessions continue referencing the original configuration.

---

# 12. Market Data

Paper must use live market data.

The implementation must reuse OpenAlgo's existing market-data architecture wherever practical.

The strategy must receive data through:

```text
PaperContext
```

rather than directly accessing:

- WebSocket clients
- REST APIs
- broker APIs
- OpenAlgo internals

The initial implementation should prefer the existing OpenAlgo live market-data transport, with WebSocket preferred if its existing architecture supports the required strategy execution semantics.

A second independent market-data transport must not be created without a demonstrated requirement.

---

# 13. Strategy Context

The common strategy contract established in the backtest engine remains unchanged.

Conceptually:

```python
strategy.decide(context) -> TargetPortfolio
```

Paper provides:

```text
PaperContext
```

which exposes:

- current market data
- current virtual positions
- available virtual capital
- configuration
- session information
- clock
- strategy state

The strategy must not directly access the broker or sandbox.

---

# 14. Paper Execution Architecture

```text
Live Market Data
       │
       ▼
Strategy Plugin
       │
       ▼
Target Positions
       │
       ▼
Portfolio Engine
       │
       ├── Allocation
       ├── Risk
       ├── Position Limits
       └── Conflict Resolution
       │
       ▼
Desired Orders
       │
       ▼
Paper Safety Boundary
       │
       ▼
OpenAlgo Sandbox
       │
       ▼
Simulated Fills
       │
       ▼
Fill Attribution
       │
       ▼
Virtual Portfolio
       │
       ▼
Ledger
```

---

# 15. Hard Paper Safety Boundary

This is a **P0 safety requirement**.

Paper orders must never reach the live broker.

The Paper execution path must be structurally distinct:

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

Paper must not call a generic execution function whose behavior depends on a global Analyzer/Live switch.

Changing an OpenAlgo global switch must not be capable of converting an active Paper session into Live execution.

---

# 16. Broker Safety Invariant

The following must be architecturally impossible:

```text
Paper Strategy
      ↓
Live Broker
```

Tests must verify this.

A Paper execution request must either:

1. execute against sandbox, or
2. fail safely.

It must never fall through to a live broker.

---

# 17. Order Identity

The platform must create its own order identity:

```text
platform_order_id
```

The OpenAlgo order ID becomes an external execution reference.

Relationship:

```text
platform_order_id
        │
        ▼
OpenAlgo order_id
        │
        ▼
Sandbox fill(s)
```

Every platform order must contain:

```text
platform_order_id
session_id
scenario_id
strategy_instance_id
strategy_id
strategy_version_id
config_version_id
symbol
side
quantity
requested_price
timestamp
```

---

# 18. Fill Attribution

Every sandbox fill must be attributable to exactly one platform order.

The platform must then attribute the fill to:

```text
Strategy
Scenario
Session
Portfolio
```

Attribution must not depend on:

- symbol alone
- timestamp alone
- side alone
- position state alone

---

# 19. Isolated Order Behavior

In isolated mode:

```text
Alpha A
    ↓
Portfolio A
    ↓
Order A
    ↓
Sandbox

Alpha B
    ↓
Portfolio B
    ↓
Order B
    ↓
Sandbox
```

The sandbox may merge actual simulated positions.

The platform must not.

Each virtual portfolio remains independent.

---

# 20. Shared Order Behavior

In shared mode:

```text
Alpha A ──┐
          ├── Shared Portfolio
Alpha B ──┘
                 │
            Net Position
                 │
             Net Order
                 │
              Sandbox
```

The portfolio engine must deterministically resolve:

- opposing positions
- capital conflicts
- leverage constraints
- exposure limits
- risk constraints

Strategy intents must remain available for attribution even if the final portfolio order differs from an individual strategy's desired order.

---

# 21. Session Model

For the initial intraday implementation:

> One trading day constitutes one Paper session.

Session lifecycle:

```text
CREATED
   ↓
STARTING
   ↓
RUNNING
   ↓
STOPPING
   ↓
COMPLETED
```

Failure path:

```text
RUNNING
   ↓
FAILED
```

Cancellation:

```text
RUNNING
   ↓
CANCELLED
```

Optional operational state:

```text
RUNNING
   ↓
PAUSED
   ↓
RUNNING
```

---

# 22. Session Events

A Paper session must explicitly handle:

- startup
- market-open readiness
- strategy initialization
- market data connection
- order submission
- fill events
- order rejection
- market-data disconnect
- sandbox disconnect
- process restart
- application restart
- strategy restart
- EOD
- square-off
- completion

---

# 23. EOD

The existing strategy configuration determines the intended exit behavior.

For an intraday strategy:

```text
Entry:
09:16

Exit:
15:10 / configured EOD
```

At EOD:

1. strategy stops generating new entries.
2. open positions are identified.
3. configured square-off policy runs.
4. sandbox fills are received.
5. virtual positions are updated.
6. P&L is finalized.
7. ledger is reconciled.
8. session becomes `COMPLETED`.

---

# 24. Restart and Recovery

A Paper session must survive an application restart where technically possible.

On restart:

1. identify incomplete sessions.
2. load session state.
3. restore virtual portfolios.
4. restore open virtual positions.
5. restore outstanding platform orders.
6. reconcile known sandbox orders/fills.
7. prevent duplicate order creation.
8. resume or safely terminate according to configured recovery policy.

No event may be duplicated because of restart/retry.

---

# 25. Ledger

Paper reuses the common ledger from Backtest.

Required events:

```text
SIGNAL
TARGET_POSITION
ORDER_INTENT
ORDER_SUBMITTED
ORDER_REJECTED
FILL
POSITION_UPDATE
FEE
SLIPPAGE
REALIZED_PNL
UNREALIZED_PNL
SESSION_EVENT
SQUARE_OFF
```

Every event contains the relevant:

```text
session_id
scenario_id
strategy_instance_id
```

---

# 26. P&L

Paper P&L must distinguish:

```text
Gross P&L
Fees
Slippage
Net P&L
Realized P&L
Unrealized P&L
```

The same accounting rules used by Backtest must be reused.

---

# 27. Backtest / Paper Parity

The same strategy version and configuration must be executable in both:

```text
BACKTEST
PAPER
```

Only environment-specific components differ.

For a deterministic fixture:

```text
same strategy
same config
same market sequence
same capital
same execution assumptions
```

must produce equivalent:

- signals
- target positions
- order intents
- fills
- P&L

within explicitly defined execution tolerances.

---

# 28. Paper Analytics

The existing run/session analytics should expose:

```text
Return
P&L
Max Drawdown
Win Rate
Trade Count
Costs
Turnover
Capital Utilization
Exposure
```

For multi-strategy sessions additionally:

```text
Strategy Contribution
Position Overlap
Conflict Count
Capital Competition
Netting Impact
```

---

# 29. UI — Strategy Registry

Each strategy should expose:

```text
Backtest
Paper
Shadow
Live
Pause
Stop
```

Selecting Paper opens the Paper configuration/session flow.

---

# 30. UI — Paper Session

Display:

```text
Session ID
Strategy / Strategies
Mode
Capital
Leverage
Status
Market Data Status
Sandbox Status
Current P&L
Current Positions
Open Orders
Last Signal
Last Fill
```

For shared mode:

```text
Portfolio
 ├── Total P&L
 ├── Net Exposure
 ├── Buying Power
 └── Positions

Strategies
 ├── Alpha A contribution
 └── Alpha B contribution
```

---

# 31. UI — Scenario Management

Allow:

```text
+ New Paper Scenario
```

Configuration:

```text
Mode:
    ISOLATED
    SHARED

Strategies:
    Alpha A
    Alpha B
    ...

Capital:
    ...

Allocation:
    ...

Leverage:
    ...
```

Multiple scenarios may be active simultaneously.

---

# 32. UI — Safety

The UI must clearly display:

```text
● PAPER
```

and:

```text
Sandbox Execution
```

Paper must never appear visually equivalent to Live.

Before starting:

```text
Execution Environment: PAPER
Broker Execution: DISABLED
Sandbox: ENABLED
```

---

# 33. Persistence

Minimum entities:

```text
paper_sessions
paper_session_strategies
paper_session_events
paper_orders
```

These should reuse common entities where possible rather than creating parallel order/fill/ledger systems.

Preferred model:

```text
sessions
orders
fills
positions
ledger_entries
```

with:

```text
environment = PAPER
```

where that fits the existing schema.

Create Paper-specific tables only when the lifecycle requires data that cannot naturally belong in the shared model.

---

# 34. Provenance

Every Paper session must record:

```text
strategy_id
strategy_version_id
artifact_hash
model_hash
adapter_version

config_version_id
config_hash

scenario_id
interaction_mode

market_data_source
market_data_transport

execution_environment
execution_model
cost_model

engine_version
created_at
started_at
completed_at
```

---

# 35. Logging

Every important Paper event must include:

```text
session_id
scenario_id
strategy_instance_id
platform_order_id
```

Example:

```text
session=paper-20261007-001
scenario=sc-04
strategy=alpha-a
order=po-123
event=FILL
```

Logs must be searchable by session.

---

# 36. Error Handling

### Market data failure

Strategy must not generate orders from stale/invalid data.

Configured behavior:

```text
PAUSE
or
FAIL_SAFE
```

### Sandbox failure

No new virtual fill may be assumed.

Orders remain pending/rejected according to actual sandbox state.

### Order rejection

The virtual portfolio must not assume the rejected position exists.

### Strategy crash

The session must preserve all previous ledger state.

### Platform restart

Session state must be recoverable.

---

# 37. Idempotency

The following operations must be idempotent:

- order submission
- fill processing
- ledger entry creation
- position update
- session completion
- EOD square-off

Retries must not create:

- duplicate orders
- duplicate fills
- duplicate ledger entries
- duplicate P&L

---

# 38. UAT

## PAPER-001 — Single Strategy

Start:

```text
Alpha A
₹5L
1x
PAPER
ISOLATED
```

Verify:

- session created
- strategy started
- virtual capital assigned
- live data received
- sandbox used
- no broker order emitted
- ledger created

---

## PAPER-002 — Isolated A/B

Start:

```text
Alpha A ₹5L
Alpha B ₹5L
ISOLATED
```

Verify:

- separate books
- separate positions
- separate P&L
- same market data
- no state leakage

---

## PAPER-003 — Shared A/B

Start:

```text
Alpha A ₹5L
Alpha B ₹5L
SHARED
```

Verify:

- common capital
- common positions
- deterministic netting
- strategy attribution

---

## PAPER-004 — Opposing Signals

Force:

```text
Alpha A → BUY 100 RELIANCE
Alpha B → SELL 100 RELIANCE
```

Verify:

- intents preserved
- net portfolio position correct
- resulting sandbox order correct
- attribution correct

---

## PAPER-005 — Concurrent Scenarios

Run:

```text
Scenario 1 → ISOLATED
Scenario 2 → SHARED
Scenario 3 → SHARED 60/40
```

Verify:

- all run concurrently
- no state leakage
- independent ledgers
- correct attribution

---

## PAPER-006 — Global Toggle Safety

While Paper is running, change OpenAlgo's global execution/analyzer setting.

Expected:

> Paper remains incapable of reaching the live broker.

---

## PAPER-007 — Direct Broker Safety

Attempt to force a broker execution from Paper context.

Expected:

> Request rejected before reaching broker execution.

---

## PAPER-008 — Restart

Restart the platform during an active session.

Verify:

- session recovered
- virtual positions recovered
- orders reconciled
- no duplicate orders
- ledger remains correct

---

## PAPER-009 — Order Rejection

Force sandbox rejection.

Verify:

- rejection persisted
- virtual position unchanged
- ledger reconciles

---

## PAPER-010 — EOD

Allow session to reach EOD.

Verify:

- configured square-off occurs
- positions close
- final P&L calculated
- ledger reconciles
- session becomes COMPLETED

---

## PAPER-011 — Backtest/Paper Fixture

Run identical deterministic strategy fixture in:

```text
BACKTEST
PAPER
```

Verify equivalence within defined execution tolerance.

---

## PAPER-012 — Strategy Crash

Terminate strategy process during a session.

Verify:

- prior ledger state preserved
- no orphaned financial events
- session enters appropriate state
- recovery behavior follows policy

---

# 39. Acceptance Criteria

Phase 8 is accepted only when:

### Strategy

- [ ] Existing strategy plugin runs unchanged.
- [ ] Strategy identity is preserved.
- [ ] Configuration identity is preserved.

### Market Data

- [ ] Live data reaches PaperContext.
- [ ] Strategy cannot directly access market-data transport.

### Execution

- [ ] Paper uses sandbox.
- [ ] Paper cannot invoke live broker.
- [ ] Orders correlate correctly.
- [ ] Fills correlate correctly.

### Portfolio

- [ ] Isolated mode works.
- [ ] Shared mode works.
- [ ] Virtual capital is enforced.
- [ ] Positions reconcile.

### Ledger

- [ ] Signals recorded.
- [ ] Orders recorded.
- [ ] Fills recorded.
- [ ] P&L reconciles.
- [ ] Strategy attribution works.

### Sessions

- [ ] Session lifecycle works.
- [ ] EOD works.
- [ ] Restart/recovery works.
- [ ] Failures are recoverable.

### Concurrency

- [ ] Multiple Paper scenarios run simultaneously.
- [ ] No state contamination.
- [ ] One scenario can fail without corrupting others.

### Safety

- [ ] BACKTEST cannot reach broker.
- [ ] PAPER cannot reach broker.
- [ ] SHADOW cannot place live orders.
- [ ] Global Live/Analyzer toggle cannot bypass Paper safety.

---

# 40. Phase 8 Implementation Sequence

Phase 8 should be implemented in the following order:

```text
8.1 Session model
        ↓
8.2 PaperContext
        ↓
8.3 Live market-data integration
        ↓
8.4 Sandbox execution adapter
        ↓
8.5 Platform order correlation
        ↓
8.6 Virtual portfolio integration
        ↓
8.7 Isolated Paper
        ↓
8.8 Shared Paper
        ↓
8.9 Concurrent scenarios
        ↓
8.10 Restart/recovery
        ↓
8.11 EOD lifecycle
        ↓
8.12 UI / analytics
        ↓
8.13 UAT
        ↓
8.14 Safety audit
```

Each sub-phase must have its own:

- implementation
- unit tests
- integration tests
- UAT cases
- acceptance criteria
- rollback procedure

---

# 41. LLD Directive

The implementation must follow the existing OpenAlgo architecture.

Do not:

- introduce another backend framework
- introduce another frontend framework
- replace OpenAlgo's sandbox
- replace OpenAlgo's market-data system
- create a second authentication mechanism
- duplicate the portfolio engine created in Phase 4
- duplicate the ledger created in Phase 4

Inspect the actual OpenAlgo repository before implementing each integration.

Where OpenAlgo already has a service, database abstraction, route pattern, strategy-host mechanism, sandbox mechanism, logging mechanism, or frontend component, reuse/extend it.

The LLD must be derived from the actual OpenAlgo implementation rather than invented independently.

---

# 42. Core Architectural Invariant

The most important Phase 8 rule is:

```text
                 SAME PLATFORM OBJECTS

Strategy
Strategy Version
Config
Strategy Instance
Scenario
Portfolio
Position
Order
Fill
Ledger
Attribution

                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
    BACKTEST       PAPER        LIVE
        │            │            │
   Historical      Live         Live
      Data         Data          Data
        │            │            │
   Simulated      Sandbox      Broker
   Execution      Execution    Execution
```

Only the environment-specific boundary changes.

---

# 43. Definition of Done

Phase 8 is complete when an operator can:

1. Register an existing strategy.
2. Select its version.
3. Select a versioned configuration.
4. Create an isolated Paper scenario.
5. Create a shared Paper scenario.
6. Assign virtual capital.
7. Run multiple scenarios simultaneously.
8. Observe live market data.
9. Generate sandbox orders.
10. Receive sandbox fills.
11. Attribute fills to strategies.
12. View virtual positions.
13. View strategy and portfolio P&L.
14. Reconcile the ledger.
15. Restart the system without corrupting the session.
16. Complete EOD square-off.
17. Retrieve the complete session history.
18. Demonstrate that no Paper path can reach the live broker.

The final safety property must be demonstrable through automated tests, not merely documented as an operational rule.

---

# 44. Phase 8 Architectural Outcome

After Phase 8, the platform should support:

```text
                    STRATEGY
                       │
                 ┌─────┴─────┐
                 │           │
              BACKTEST      PAPER
                 │           │
           Historical      Live Data
                 │           │
          Simulated Exec   Sandbox
                 │           │
                 └─────┬─────┘
                       │
                 SAME PORTFOLIO
                  + LEDGER MODEL
                       │
                       ▼
                    SHADOW
                       │
                       ▼
                     LIVE
```

The critical result is not merely "Paper trading works."

The result is:

> **A strategy can move from historical simulation to live-data simulation without changing its identity, portfolio semantics, configuration, or ledger model, while the execution environment changes underneath it.**

That is the foundation required for a trustworthy Backtest → Paper → Shadow → Live lifecycle.
