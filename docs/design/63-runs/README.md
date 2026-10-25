# Runs: strategies trading together with their own money (fork)

A Run is a scenario to trade in the Sandbox: several Python strategies trading
together there, with a capital, a mode, a P&L policy and a limit of
their own. Runs are built on Sandbox accounts (`design/62-sandbox-accounts`)
and need `SANDBOX_ACCOUNTS_ENABLED=true`.

Code: `services/run_service.py` (defining, starting, the daily open and
close), `sandbox/copy_engine.py` (copying a source's trades into members'
accounts), `portfolio/run_capital.py` (the money rules, no I/O), `portfolio/run_sim.py`
(a Run backtested over replayed fills by the same rules, no I/O),
`blueprints/runs.py` (the JSON the pages call), `add_source` and
`remove_source` in `blueprints/python_strategy.py`. Tests:
`test/research/test_runs.py`, `test/sandbox/test_copy_engine.py`,
`test/test_run_capital.py`, `test/test_run_sources_host.py`, `test/test_run_sim.py`,
`frontend/src/pages/runs/*.test.tsx`.

## A Run's definition

| Setting | Choices |
| --- | --- |
| Capital | The Run's capital, in rupees. |
| Mode | **Isolated**: each member trades an account of its own. **Shared**: the Run has one account; members' positions net in it, and each member's share is read from the orders carrying its name. Fixed for the Run's life. |
| P&L policy | What a member starts each day with. **Reset**: its allocation again; a profit is swept out and a loss topped up. **Own**: it keeps its own result, so a winner grows and a loser shrinks without touching the other. **Pooled**: the members' balances are added up and shared out again by allocation. Own is for Isolated Runs only. |
| Limit | The most a member may hold, as total exposure across every symbol. **Capital**: its capital that day, so it grows with profits. **Fixed**: its allocation, whatever it holds. |
| Daily loss limit | Optional, a % of capital. Each morning every account of the Run is given that share of the capital it opened the day with: each member's own in an Isolated Run, the whole Run's in a Shared one. Past it, the account's intraday positions are squared off and its copies are refused for the rest of the day. The Sandbox's own loss limit still applies; the nearer one decides. |
| Members | Python strategies at /python, each with an allocation (an amount, or a share of the Run's capital; they need not add up to 100%) and a sizing: **platform** (copies scale with capital) or **self** (the strategy's own quantity, within the limit). |

Each member is frozen when the Run is defined: the version of its script that
Research keeps (by file hash), its schedule, exchange and symbols. Editing the
script at /python does not change a Run. An edit to the Run adds a version
that applies from the next trading day; the day that is trading finishes on
the version it started with. Versions cannot be changed once written.

Worked example (Rs 5 lakh, Isolated; A is allocated Rs 3 lakh, B 40%). Day
one A nets +10,000 and B -20,000. Day two opens with:

| Policy | A | B |
| --- | --- | --- |
| Reset | 3,00,000 | 2,00,000 |
| Own | 3,10,000 | 1,80,000 |
| Pooled (4,90,000 shared out) | 2,94,000 | 1,96,000 |

## One process per configuration

Members whose configurations are identical (the same version, schedule,
exchange, symbols and sizing; not the capital) share one **source**, even
across Runs. The source is an entry in the strategy host, `src_<12 hex>`,
running a pinned copy of the version in a Sandbox account of its own at a
reference capital: at least Rs 1 crore, so members scale down from it. It is
handed `OPENALGO_STRATEGY_CAPITAL` (the reference capital),
`OPENALGO_STATE_DIR` (a directory of its own, since two sources of one script
can run at once), `OPENALGO_SYMBOLS` (its frozen symbols) and an account key.
Its `STRATEGY_NAME` is `<name> #<6 hex>` and never changes during a day.
Sources do not appear in /python and its routes refuse them.

After every fill in a source's account, each member's account gets one MARKET
order for the difference between the position it should hold (the source's,
scaled to its capital and rounded toward zero, within its limit) and the one
it holds. The order is placed with the source's fill price as its quote, so
it fills at once at that price, through the normal Sandbox checks, and is
charged on the member's own quantity. A member whose share rounds to zero
holds nothing. A refused copy is caught up by the next fill. Each source trade
is copied once per member, whoever asks. The source's own square-off is not
copied: every account is squared off on its own.

## Days

At 09:05 every active Run's day opens: each seat's capital is decided by the
policy and recorded as the day's rows, then moved as transfers that carry a
reference (so a restart part-way repeats nothing), each member's copy is
pointed at that day's scale and limit, and each account is given that day's
loss limit. At 15:40 the day is booked from each
account: its closing value, its charges and its net. Sources no Run trades the
next day are stopped. Both run again after a restart if they were missed.

## A Run's life

- **Start**: from now if today is trading; sources inside their window start
  at once.
- **Pause**: no new copies; what the accounts hold stays until the
  square-off. Start again to resume.
- **Kill**: pause and close everything the Run's accounts hold, now.
- **Stop**: for good; the accounts keep their history.
- **Delete**: only a Run that never traded. One that did keeps its record.

API: `GET/POST /runs/api/runs`, `GET/PUT/DELETE /runs/api/runs/<id>`,
`POST /runs/api/runs/<id>/start|pause|kill|stop`.

## P&L

`GET /runs/api/pnl` answers the P&L page: one daily series per thing asked
for, each with its totals, and all of them added up by day.

| Parameter | Series |
| --- | --- |
| `runs=1,2` | Each Run as a whole: its seats summed by day. |
| `members=1:<seat>` | One strategy within its Run, also one since removed from it. Isolated: its own account. Shared: its own orders in the Run's account and its share of the square-off, charged by the same schedule the account is, and its share of the Run's opening capital by the allocations that day traded on. |
| `main=1` | The main Sandbox account: everything it traded, as one book, strategies outside Runs and manual orders alike. |
| `strategies=python:<id>,...` | A strategy outside Runs, as the Strategy Dashboard names it. |
| `from`, `to` | Inclusive dates. |

Each day carries its opening capital, the transfer at the open, gross,
charges, net, trades and the running total. A booked day comes from the day's
row; today, before the 15:40 close, is read live from the account. Totals and
drawdown are `services/strategy_performance_service.daily_summary`, measured
on the opening capital of the first day shown.

## Backtests

`POST /runs/api/runs/<id>/backtests {from, to}` backtests the Run as it is
defined now, an edit from tomorrow included, over past days.
`GET /runs/api/runs/<id>/backtests` lists them, newest first, and
`GET /runs/api/runs/<id>/backtests/<bid>` returns one with its days in the P&L
API's shape, the whole Run and each member, for the Compare page.

- **One replay per configuration.** Each configuration among the members is
  replayed once (`research.script_replay`) as an ordinary Research backtest of
  the pinned version, on its own schedule, exchange and symbols, at its
  source's reference capital, handed that same capital every day as a live
  source is (`ScriptSettings.fixed_capital`). Another Run sharing the
  configuration and the days reuses the replay, and so does a second backtest
  of the same days. The replays are listed with the script's own Research
  backtests, named "(Run source)".
- **Then the Run, simulated.** When every replay is done,
  `portfolio/run_sim.py` trades the Run over their fills by the Sandbox's own
  rules: scaling and rounding, the exposure limit at cost, Shared netting,
  charges, the P&L policy and its transfers, the daily loss limit and the
  15:15 square-off. It books as the Sandbox books, an average kept to the
  paise, and `test_a_simulated_day_books_as_the_sandbox_does` holds the two to
  the paise on the same fills, Isolated and Shared.
- **Every trading day** of the window opens and closes, a day without fills
  too, so a backtest's days line up with the Run's Sandbox days.
- A replay that fails fails the backtest with its reason; one waiting on bars
  the broker is downloading keeps it waiting. A backtest is settled when it is
  next read.

Not modelled in a backtest: the daily loss limit is checked at each fill, not
each minute, as the replay's minute bars stay with its worker; a copy lands at
its source's price with no delay, as the Sandbox prices it; margin, which the
exposure limit keeps within capital.

## Pages

All under `/runs`, with tabs to Strategies (`/python`), Runs, P&L, Compare and
Accounts, and in the profile menu as Runs and P&L. Code:
`frontend/src/pages/runs/`, `frontend/src/api/runs.ts`, and
`components/portfolio/DailyPnlCalendar.tsx`.

| Page | What it shows |
| --- | --- |
| `/runs` | Every Run: status, mode and policy, strategies, capital and net today, net since start; the sources trading for Runs. Tick two to compare. |
| `/runs/new`, `/runs/<id>/edit` | The definition, with the allocation total and two previews worked out by the Run's own rules: day one's capital, limit and copy scale per strategy, and day two's capital by each policy for a day-one result the trader can change. |
| `/runs/<id>` | Today: capital, net and charges so far, holdings against the limit, a loss limit reached, each strategy's copies (filled and refused, with the reason), what each account holds, the sources copied and today's events. Then its days (the P&L report), its backtests beside its Sandbox days, and its definition and history. Edit, Start or Resume, Pause, Stop, Kill switch (asks first), Delete for a draft. |
| `/runs/pnl` | The P&L report for any mix of Runs, one strategy within a Run, the main account and the strategies outside Runs, over Today to All or custom days, each on its own or combined: headline figures, cumulative net, each day's net, drawdown, who made it, by month, a calendar of days with the chosen day's figures beside it, and every day as a table with its CSV. |
| `/runs/compare` | Runs over the days they all traded, as a share of what each opened with: return and drawdown, side by side, each strategy's share, and a backtest of one beside its Sandbox days. |
| `/runs/accounts` | The main account and every account: what it started with, what it is worth now, charges today, its daily loss limit, and what moved its money. |

The Strategy Dashboard (`/strategy-dashboard`) stays: its per-strategy
ledgers have no place on the P&L page, which links to it.

## Not yet

- Members trade NSE and BSE equity intraday only.
- A source's own orders are not held to a limit; the members' copies are.
- In a Shared Run, two strategies holding opposite sides of one symbol at the
  15:15 square-off net in the account, and the square-off closes only the
  difference. Each strategy's own figures leave that netted part out for the
  day; the Run's total, read from the account, is exact.
- A strategy whose decisions depend on its capital gets a source per member,
  at that member's allocation; under Own or Pooled its capital moves from
  there, and its copies are scaled from the allocation.
