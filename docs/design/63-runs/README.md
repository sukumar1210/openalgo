# Runs: strategies trading together with their own money (fork)

A Run is a scenario to paper-trade: several Python strategies trading
together in the Sandbox, with a capital, a mode, a P&L policy and a limit of
their own. Runs are built on Sandbox accounts (`design/62-sandbox-accounts`)
and need `SANDBOX_ACCOUNTS_ENABLED=true`.

Code: `services/run_service.py` (defining, starting, the daily open and
close), `sandbox/copy_engine.py` (copying a source's trades into members'
accounts), `portfolio/run_capital.py` (the money rules, no I/O),
`blueprints/runs.py` (the JSON the pages call), `add_source` and
`remove_source` in `blueprints/python_strategy.py`. Tests:
`test/research/test_runs.py`, `test/sandbox/test_copy_engine.py`,
`test/test_run_capital.py`, `test/test_run_sources_host.py`.

## A Run's definition

| Setting | Choices |
| --- | --- |
| Capital | The Run's capital, in rupees. |
| Mode | **Isolated**: each member trades an account of its own. **Shared**: the Run has one account; members' positions net in it, and each member's share is read from the orders carrying its name. Fixed for the Run's life. |
| P&L policy | What a member starts each day with. **Reset**: its allocation again; a profit is swept out and a loss topped up. **Own**: it keeps its own result, so a winner grows and a loser shrinks without touching the other. **Pooled**: the members' balances are added up and shared out again by allocation. Own is for Isolated Runs only. |
| Limit | The most a member may hold, as total exposure across every symbol. **Capital**: its capital that day, so it grows with profits. **Fixed**: its allocation, whatever it holds. |
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
reference (so a restart part-way repeats nothing), and each member's copy is
pointed at that day's scale and limit. At 15:40 the day is booked from each
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

## Not yet

- No pages: the Runs and P&L pages come next, after their wireframes are
  approved.
- A member's daily loss limit is recorded with the Run but not yet enforced;
  the Sandbox's own loss limit (a setting for every account) still applies.
- Members trade NSE and BSE equity intraday only.
- A source's own orders are not held to a limit; the members' copies are.
- In a Shared Run, two strategies holding opposite sides of one symbol at the
  15:15 square-off net in the account, and the square-off closes only the
  difference. Each strategy's own figures leave that netted part out for the
  day; the Run's total, read from the account, is exact.
- A strategy whose decisions depend on its capital gets a source per member,
  at that member's allocation; under Own or Pooled its capital moves from
  there, and its copies are scaled from the allocation.
- No backtest of a Run yet.
