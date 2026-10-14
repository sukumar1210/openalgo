# Strategy dashboard (fork)

`/strategy-dashboard` compares every strategy's **Sandbox trading** on one page:
Python strategies, strategy-module strategies (`/strategy`) and any other order
name. Each strategy has a daily P&L curve, its totals and its ledger. The chosen
strategies can be drawn individually or combined as one account.

## Where the numbers come from

Every Sandbox order carries the name of the strategy that sent it
(`SandboxTrades.strategy`), so the Sandbox's trades are already one ledger for
every kind of strategy. `services/strategy_performance_service.py` groups the
names into strategies:

| Strategy | Its order names |
| --- | --- |
| Python strategy | the names learned from when it ran (`research_script_links.learned_tags`) and its own name (`STRATEGY_NAME`) |
| Strategy-module strategy | its name (`order_dispatch.build_order` puts it on every order) |
| Anything else | each name is a row of its own |

A name is claimed once, in that order, so the same fills never sit in two rows
or twice in a combined curve. Research's own session names (`research:`) and
the Sandbox's square-off (`AUTO_SQUARE_OFF`) are never rows.

Fills and P&L come from `portfolio/strategy_pnl.py`. That is the same rule the
Sandbox P&L page's strategy filter and the Python strategy Sandbox days tab use,
so the three agree:

- each strategy's own orders, plus its share of the 15:15 square-off (up to
  what it held, in the closing direction);
- realized P&L from its own average price, by trade date, after the charges a
  Sandbox day is costed with (`fill_charges`: NSE and BSE equity, intraday
  unless the product is CNC). A fill on another exchange has no schedule; it
  is counted before charges, and the table says how many there were.

## Views

- **Individual:** each checked strategy's cumulative daily P&L as its own line.
- **Combined:** the checked strategies' daily P&L added together, as the one
  Sandbox account they share. It shows the combined drawdown and each
  strategy's share of the total.
- **Ledger:** one strategy's fills, newest first, with square-off fills marked.

Drawdown is measured on the cumulative daily net P&L: in rupees, and as a
share of capital (`research.metrics.max_drawdown`). A Python strategy is
measured on its own capital when the window opens: its starting capital (its
Schedule tab, Rs 5,00,000 unless set) plus its own net P&L of every earlier
day, which is also what the host hands it as `OPENALGO_STRATEGY_CAPITAL`
(`capital_on`). Other strategies are measured on the Sandbox's capital.

## Limits

- Sandbox trading only. A Python strategy in Live mode trades at the broker,
  where there is no ledger of its own yet. Live strategy-module runs are not
  shown.
- The curve has one point per day, of realized P&L. A position carried
  overnight counts only when it is closed.
- In a shared, netted account, two strategies holding opposite positions in
  one stock leave the account flat, so the square-off closes neither. Each is
  then shown still holding its side.
