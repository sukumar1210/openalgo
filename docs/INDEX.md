# OpenAlgo Documentation Map

The entry point for humans and AI agents. This file is a **map, not a copy** —
it points at the canonical docs that already live under `docs/`. Edit a source
doc once; everything that reads through this map sees the change immediately.

**How to use (progressive disclosure):** read this map → open the one area you
need → drill into the specific file. Don't load everything at once.

---

## Using OpenAlgo (product, API, SDK)

| Read this when you need… | Entry point |
|---|---|
| The REST API (`/api/v1/`) — orders, market data, options, account, streaming | [api/README.md](api/README.md) |
| The Python SDK — install, client, data/order/account calls | [<prompt/openalgo python sdk.md>](<prompt/openalgo python sdk.md>) |
| Symbol format across exchanges (equity/futures/options) | [prompt/symbol-format.md](prompt/symbol-format.md) |
| Order constants (exchange / product / price-type / action codes) | [prompt/order-constants.md](prompt/order-constants.md) |
| Contract lot sizes | [prompt/LotSize.md](prompt/LotSize.md) |
| WebSocket subscription & message format | [prompt/websockets-format.md](prompt/websockets-format.md) · [prompt/websockets-verbose-control.md](prompt/websockets-verbose-control.md) |
| Service-layer functions & Flow JSON import | [prompt/services_documentation.md](prompt/services_documentation.md) · [prompt/flow-import-format.md](prompt/flow-import-format.md) |
| Strategy module & risk engine (multi-leg options, signal mode, RMS) | [prompt/strategy_rms_documentation.md](prompt/strategy_rms_documentation.md) · [api/strategy-services/](api/strategy-services/) · [prd/strategy-module-rms.md](prd/strategy-module-rms.md) · [bdd/strategy_module_rms.feature](bdd/strategy_module_rms.feature) |
| Runs (fork): Python strategies trading together in the Sandbox with their own capital, P&L policy and limits; one source per configuration, copied into members' accounts; backtests by replay and simulation; the Runs, P&L (with its calendar), Compare and Accounts pages | [design/63-runs/README.md](design/63-runs/README.md) |
| Running the OpenAlgo container as its own uid (fork, proposal): why the strategy process limit couples to the host user, the uid-only change, everything that reads or writes the state, migration, rollback and the planted check | [design/64-dedicated-uid/README.md](design/64-dedicated-uid/README.md) |
| Sandbox accounts (fork): money of its own inside one Sandbox, reached by an account key, charged per fill; off unless SANDBOX_ACCOUNTS_ENABLED | [design/62-sandbox-accounts/README.md](design/62-sandbox-accounts/README.md) |
| Strategy dashboard (`/strategy-dashboard`, fork): every strategy's Sandbox trading compared, individually or as one account, with ledgers | [design/60-strategy-dashboard/README.md](design/60-strategy-dashboard/README.md) |
| Research backtests (`/research`, fork): strategy versions, experiments, isolated and shared simulation, and Research as tabs on each Python strategy at /python (unchanged-script backtests, recorded Sandbox days, results) | [design/57-research-backtest/README.md](design/57-research-backtest/README.md) · [prd/research-backtest.md](prd/research-backtest.md) |
| Research Sandbox (`/research`, fork): deployed scenarios trading live market data with sandbox fills | [design/58-research-sandbox/README.md](design/58-research-sandbox/README.md) · [prd/research-sandbox.md](prd/research-sandbox.md) |
| Research platform (fork): the master PRD, the Phase 4 gate review, the plan and status for the ledger, analytics, Shadow, Live and hardening, the item-by-item verification, and the Alpha A adapter brief | [design/59-research-platform/README.md](design/59-research-platform/README.md) · [verification](design/59-research-platform/verification.md) · [Alpha A brief](design/59-research-platform/alpha-a-adapter.md) · [prd/research-platform.md](prd/research-platform.md) |
| Technical indicators (`ta` library) | [<prompt/indicators/openalgo indicators - introduction.md>](<prompt/indicators/openalgo indicators - introduction.md>) |
| The charting terminal at `/trading`, its order dock and its shortcuts | [userguide/32-charting-terminal](userguide/32-charting-terminal/README.md) |
| Writing your own chart indicators for `/trading` | [custom-indicators.md](custom-indicators.md) |
| Writing studies in OpenScript for `/trading` | [openscript.md](openscript.md) |
| Step-by-step user guide (setup → first order → integrations) | [userguide/README.md](userguide/README.md) |
| MCP tool reference (Claude Desktop / Cursor / Windsurf) | [mcp-tool-reference.md](mcp-tool-reference.md) |

## Install, deploy & operate

| Topic | Entry point |
|---|---|
| Ubuntu server install | [installation-guidelines/getting-started/ubuntu-server-installation.md](installation-guidelines/getting-started/ubuntu-server-installation.md) |
| Docker | [docker/README.md](docker/README.md) |
| Optional gthread web server: fresh installs, switching on Ubuntu and Docker, verification and rollback | [gthread/README.md](gthread/README.md) |
| Upgrade / SMTP / TOTP / forgot-password | https://docs.openalgo.in/installation-guidelines/getting-started/ |
| Broker integration (36 plugins) | [broker-integration-guide.md](broker-integration-guide.md) |
| Release notes & changelog | [releases/](releases/) · [CHANGELOG.md](CHANGELOG.md) |

## Feature surfaces

| Feature | Entry point |
|---|---|
| Agent (`/agent`) | [design/55-agent/README.md](design/55-agent/README.md) |
| Scalping Terminal (`/scalping`) | [scalping/PRD.md](scalping/PRD.md) |
| Scanner architecture | [scanner-architecture.md](scanner-architecture.md) |
| WhatsApp alerts | [whatsapp.md](whatsapp.md) |
| Telegram chart rendering | [telegram-chart-rendering.md](telegram-chart-rendering.md) |
| Health monitoring | [HEALTH_MONITORING_IMPLEMENTATION.md](HEALTH_MONITORING_IMPLEMENTATION.md) · [HEALTH_MONITOR_REACT_FRONTEND.md](HEALTH_MONITOR_REACT_FRONTEND.md) |

## Architecture, design & specs (contributors)

| Topic | Entry point |
|---|---|
| First-time contributor setup (devsprint prep) | [devsprint/README.md](devsprint/README.md) |
| System design (frontend, backend, DB, UI) | [design/README.md](design/README.md) |
| Product requirements — Flow, Python strategies, Strategy module & RMS, Sandbox, Historify, MCP, event bus, websocket proxy | [prd/README.md](prd/README.md) · [prd/PRD.md](prd/PRD.md) |
| BDD feature specs (Gherkin `.feature`) | [bdd/README.md](bdd/README.md) |
| WebSocket architecture & quote feed | [websocket-architecture.md](websocket-architecture.md) · [websocket-quote-feed.md](websocket-quote-feed.md) |
| Security audits | [audit/README.md](audit/README.md) |
| CI/CD | [prd/ci-cd.md](prd/ci-cd.md) |
| Benchmarks | [benchmarks/](benchmarks/) |
| Migration plans | [migration/](migration/) |
| Implementation plans | [plans/](plans/) |
| Testing guides | [test/](test/) |
| XTS API | [xtsapi.md](xtsapi.md) |

---

## Governance

- User responsibilities & risk ownership: https://docs.openalgo.in/responsibilities
- Repository: https://github.com/marketcalls/openalgo · Docs: https://docs.openalgo.in
