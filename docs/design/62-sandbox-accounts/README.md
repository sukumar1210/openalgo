# Sandbox accounts (fork)

Money of its own inside one user's Sandbox, so that scenarios can trade side
by side without one's loss spending another's money. The foundation for Runs
(several strategies run together with their own capital policy), which open
and fund these accounts. Off unless `SANDBOX_ACCOUNTS_ENABLED` is `true`.

Code: `sandbox/accounts.py` (the registry, keys to principals, money events,
per-fill charges), `sandbox/engines.py` (when the Sandbox's engines run),
`upgrade/migrate_sandbox_accounts.py`. Tests:
`test/sandbox/test_sandbox_accounts.py`.

## How an account is separate

The Sandbox keys every row by a `user_id` string: funds, orders, trades,
positions, holdings, GTTs and daily P&L. An account is a registered principal,
`<owner>~<token>` (8 hex digits), used as that `user_id`. Margin, fills, the
square-off, T+1 settlement and the daily loss limit then apply to it on its
own. Nothing in the Sandbox engine needed to learn about accounts for that.

| Table | What it holds |
| --- | --- |
| `sandbox_accounts` | Each account: principal, owner, purpose (`run`, `member`, `source`), starting capital, whether fills are charged, its own daily loss limit, status (`active` or `closed`). |
| `sandbox_account_events` | Money moved by anything but a trade's own P&L: opening, deposits, withdrawals, transfers, per-fill charges. Written in the same commit as the funds change it records. |

## A daily loss limit of its own

An account can carry a daily loss limit in rupees
(`accounts.set_daily_loss_limit`); a Run sets one on each of its accounts every
morning (`design/63-runs`). The Sandbox's loss-limit check
(`design/07-sandbox`, MIS Loss Limit) enforces it beside the Sandbox's own
percentage: the nearer of the two applies, and an order it refuses names that
one. An account's loss is counted after the charges it paid on its fills, as
its P&L is everywhere else; a position's own P&L leaves them out. A source account is held to neither: it is followed, and every account
copying it is held to its own. While Sandbox accounts are on, the check is
scheduled even with the Sandbox's own limit at 0, and returns at once while
no account has a limit.

## Reaching an account

An **account key** is `sandbox-<token>-<HMAC(pepper, real_key|token)>`. Only the
owner's real API key can produce it, and the token is inside the HMAC, so a
key cannot be moved to another account by editing its prefix. It starts with
`sandbox-`, so every order path already sends it to the Sandbox whatever
Analyzer mode says (`utils/strategy_mode.py`).

`services/sandbox_service.get_user_id_from_apikey` turns an account key into
its principal only while the registry holds that account open. Anything else
is `None`, which every Sandbox entry point refuses with 403. A test calls each
entry point with a key for an account that does not exist.

## Trust comes from the registry

- **No ghost accounts.** A principal of an account's shape that the registry
  does not hold gets no funds row (`FundManager.initialize_funds` refuses it).
  An account's funds are opened with the account, at its own capital.
- **The owner's feed and order stream.** An account's orders and positions
  subscribe market data through its owner's connection. The WebSocket engine
  counts references per owner, so one account letting go of a symbol cannot
  unsubscribe it for another account, or for the owner. An account's order
  updates arrive on its owner's order stream.
- **`owner_of` reads the owner off the principal's shape**, without a database
  read, because the WebSocket engine calls it under a real lock where no I/O is
  allowed. Only the registry creates principals of that shape. A username that
  itself ends in `~` and 8 hex digits is not supported.
- **Research Sandbox sessions refuse account keys.** They place into the
  owner's main Sandbox, which an account must not trade on its behalf.

## A strategy's own account

A Python strategy can trade an account of its own (kind `strategy`), so two
strategies holding the same stock never net in the main Sandbox: one's exit
would halve the position the other believes it holds, and each one's position
check would read a book that is not its own.

- **Binding** (`POST /python/account/<id>`, a label and a capital, while the
  strategy is stopped) opens the account and sets the strategy to Sandbox mode
  with `account_token` in its config, as a Run's source carries. The label is
  lowercase letters, digits and underscores, unique among the owner's open
  strategy accounts: it is what pages and algo-trading's run declarations call
  the account.
- **Its runs** are handed the account key (`_strategy_api_key`) and the
  account's worth as `OPENALGO_STRATEGY_CAPITAL` (`_capital_env`). A strategy
  without an account is launched exactly as before.
- **The loss limit** is the Sandbox's own percentage of the account's own
  capital: `squareoff_manager._limits` exempts only sources. The 15:15 MIS
  square-off sweeps every principal at the same time.
- **Refused while bound:** switching to Live, deleting the strategy, starting a
  run whose account has been closed elsewhere (it would otherwise fall back to
  the main Sandbox unannounced), and any change while it runs.
- **Funds** (`POST /python/account/<id>/funds`, positive or negative) and
  **closing** (`POST /python/account/<id>/close`) go through `move_money` and
  `close`, with their refusals. After closing, the strategy trades the main
  Sandbox again.

Its settings (`POST /python/settings/<id>`) ride beside it, for a bound
strategy only. The names are the explicit set `STRATEGY_SETTING_NAMES`
(`ALGO_PAPER_MODEL`, `ALGO_PAPER_RUN`, `ALGO_PAPER_EXIT_TIME`,
`ALGO_PAPER_DISASTER_STOP_PCT`, `ALGO_SHADOW`), never a pattern: algo-trading
reads other `ALGO_` names, such as `ALGO_LIVE_UNATTENDED_CONFIRM` and
`ALGO_DATA_DIR`, that must stay a deliberate `.env` choice. They are re-checked
at every start, and a stored name outside the set refuses the start rather than
being dropped. They are merged into the run's environment before the host's own
variables, which always win. Closing the account clears them. The frontend is
sent the set (`setting_names`) and keeps no copy.

Binding is refused while the strategy still holds a position or a resting order
in the main book (`open_sandbox_positions`, `open_sandbox_orders`), since its runs
would no longer see them.

The binding's state comes from the registry row (`AccountState`): unbound, open,
closed, or unavailable. Unavailable (accounts turned off, no owner recorded, no
row, a failed read) is its own answer and never counts as closed: it refuses
close, delete, funds, a second bind and a start, so an account that may still
hold money is never unbound by mistake. Bind records the session user as the
owner of a legacy strategy with none, so the account and every later lookup use
one owner.

## Money

- **Charges are taken in the fill.** An account opened with charges on
  (the default) is charged on every fill: the schedules a Sandbox day is
  costed with (`portfolio/strategy_pnl.fill_charges`: NSE and BSE equity,
  intraday unless the product is CNC). The charge leaves the cash and the
  realized P&L (today's included, so the day's loss limit sees it) in the
  fill's own commit, with an event naming the trade. A charge that cannot be
  worked out costs the charge, never the fill. The main Sandbox is never
  charged.
- **Deposits and withdrawals** (`move_money`) move the capital and the
  available cash together, as one event. A withdrawal beyond the free cash is
  refused.
- **The main Sandbox's own routines leave accounts alone.** The weekly reset,
  and moving every account onto a new starting capital from the Sandbox
  settings, touch main accounts only: an account's money follows its own
  capital policy (Runs).
- **Closing** an account is refused while it holds a position, a resting
  order or an active GTT.

## When the engines run

The execution engine fills resting orders and the square-off scheduler closes
intraday positions. They used to follow Analyzer mode alone. A Python strategy
in Sandbox mode and an open account trade in the Sandbox with Analyzer mode
off, so the engines now also start for them: at app start, when a strategy
switches to Sandbox mode, and after a Sandbox reset. Switching Analyzer mode
off leaves them running while either still needs them.

## Three faults fixed alongside

- **Cancel-all missed stop orders.** It looked for `trigger_pending`; SL and
  SL-M orders rest as `trigger pending`.
- **A fill released other resting orders' margin.** The consistency check
  after every fill counted open positions only, and the reconcile it ran
  counted positions and GTTs but not resting orders. The margin held by every
  other resting order was released to available cash, and when those orders
  filled, used margin came up short. Both now count positions, resting orders
  and active GTTs (`held_margin`).
- **The scheduled reset left resting orders and GTTs holding margin.** It
  zeroed used margin but left them open; cancelling one later released margin
  that no longer existed, and available cash rose above capital. The reset now
  cancels them, GTT legs included.

## Not yet

- The Sandbox pages show the main account. The Accounts page under Runs lists
  every account; a strategy's own is opened, funded and closed from its
  Schedule tab.
- An account's order updates carry no account field on the owner's stream;
  order ids are unique, and nothing reads such a field yet.
