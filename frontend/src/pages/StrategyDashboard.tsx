// pages/StrategyDashboard.tsx
// Every strategy's Sandbox trading on one page: Python strategies, strategy-
// module strategies and any other order name. Individually, each strategy's
// cumulative P&L is its own line; combined, the chosen ones are added into the
// one account they share. A strategy's ledger opens below the table.

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { formatPct } from '@/api/research'
import {
  getDashboard,
  getLedger,
  type StrategyKind,
  type StrategyPerformance,
  strategyDashboardKeys,
} from '@/api/strategy-dashboard'
import { PortfolioLineChart, type SeriesSpec } from '@/components/portfolio/PortfolioLineChart'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn, makeFormatCurrency } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'

const COLORS = ['#3b82f6', '#22c55e', '#f59e0b', '#8b5cf6', '#ef4444', '#14b8a6', '#ec4899']

const KIND_LABEL: Record<StrategyKind, string> = {
  python: 'Python strategy',
  module: 'Strategy module',
  other: 'Other orders',
}

type View = 'individual' | 'combined'

function pnlColor(value: number | null | undefined): string {
  if (!value) return ''
  return value > 0 ? 'text-green-600 dark:text-green-500' : 'text-red-600 dark:text-red-500'
}

export default function StrategyDashboard() {
  const { user } = useAuthStore()
  const money = useMemo(() => makeFormatCurrency(user?.broker), [user?.broker])
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [view, setView] = useState<View>('individual')
  // Unchecked strategies are remembered, so new ones appear checked.
  const [hidden, setHidden] = useState<string[]>([])
  const [ledgerKey, setLedgerKey] = useState<string | null>(null)

  const base = useQuery({
    queryKey: strategyDashboardKeys.data(from, to, []),
    queryFn: () => getDashboard(from, to, []),
  })
  const rows = base.data?.strategies ?? []
  const chosen = rows.filter((row) => !hidden.includes(row.key))
  const chosenKeys = chosen.map((row) => row.key)
  const combined = useQuery({
    queryKey: strategyDashboardKeys.data(from, to, chosenKeys),
    queryFn: () => getDashboard(from, to, chosenKeys),
    enabled: view === 'combined' && chosenKeys.length > 0,
  })

  const colorOf = (key: string) => COLORS[rows.findIndex((row) => row.key === key) % COLORS.length]
  const series: SeriesSpec[] =
    view === 'combined'
      ? combined.data?.combined
        ? [
            {
              name: 'Combined',
              color: '#3b82f6',
              area: true,
              data: combined.data.combined.daily.map((d) => ({
                date: d.date,
                value: d.cumulative,
              })),
            },
          ]
        : []
      : chosen.map((row) => ({
          name: row.name,
          color: colorOf(row.key),
          data: row.daily.map((d) => ({ date: d.date, value: d.cumulative })),
        }))

  const toggle = (key: string) =>
    setHidden((keys) => (keys.includes(key) ? keys.filter((k) => k !== key) : [...keys, key]))

  return (
    <div className="container mx-auto space-y-6 px-4 py-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Strategy dashboard</h1>
        <p className="text-muted-foreground">
          Every strategy's Sandbox trading: Python strategies, strategy-module strategies and any
          other order name. P&L is realized by trade date, as the Sandbox computes it, before
          charges.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="sd-from">From</Label>
          <Input id="sd-from" type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="sd-to">To</Label>
          <Input id="sd-to" type="date" value={to} onChange={(e) => setTo(e.target.value)} />
        </div>
        <div className="flex gap-1" role="radiogroup" aria-label="View">
          {(['individual', 'combined'] as const).map((v) => (
            <Button
              key={v}
              role="radio"
              aria-checked={view === v}
              variant={view === v ? 'default' : 'outline'}
              onClick={() => setView(v)}
            >
              {v === 'individual' ? 'Individual' : 'Combined'}
            </Button>
          ))}
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{view === 'individual' ? 'Each strategy' : 'As one account'}</CardTitle>
          <CardDescription>
            {view === 'individual'
              ? 'Cumulative P&L of each checked strategy, on its own.'
              : 'The checked strategies added together day by day, as the one Sandbox account they share.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {base.error ? (
            <p className="text-sm text-destructive">
              Could not load strategy performance. Refresh the page to try again.
            </p>
          ) : series.length && series.some((s) => s.data.length) ? (
            <PortfolioLineChart series={series} format={money} />
          ) : (
            <p className="py-10 text-center text-sm text-muted-foreground">
              {base.isLoading
                ? 'Loading...'
                : 'No Sandbox trades in this window for the checked strategies.'}
            </p>
          )}
          {view === 'combined' && combined.data?.combined && (
            <CombinedSummary data={combined.data.combined} money={money} />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Strategies</CardTitle>
          <CardDescription>
            Check a strategy to put it on the chart. Its ledger opens below.
          </CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead />
                <TableHead>Strategy</TableHead>
                <TableHead className="text-right">P&L</TableHead>
                <TableHead className="text-right">Days (won)</TableHead>
                <TableHead className="text-right">Best / worst day</TableHead>
                <TableHead className="text-right">Max drawdown</TableHead>
                <TableHead className="text-right">Trades</TableHead>
                <TableHead>Last day</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <StrategyRow
                  key={row.key}
                  row={row}
                  color={colorOf(row.key)}
                  checked={!hidden.includes(row.key)}
                  onToggle={() => toggle(row.key)}
                  onLedger={() => setLedgerKey(ledgerKey === row.key ? null : row.key)}
                  ledgerOpen={ledgerKey === row.key}
                  money={money}
                />
              ))}
            </TableBody>
          </Table>
          {!base.isLoading && rows.length === 0 && (
            <p className="py-6 text-center text-sm text-muted-foreground">
              No strategy has traded in the Sandbox yet.
            </p>
          )}
        </CardContent>
      </Card>

      {ledgerKey && (
        <Ledger
          strategyKey={ledgerKey}
          name={rows.find((r) => r.key === ledgerKey)?.name ?? ''}
          money={money}
        />
      )}
    </div>
  )
}

function StrategyRow({
  row,
  color,
  checked,
  onToggle,
  onLedger,
  ledgerOpen,
  money,
}: {
  row: StrategyPerformance
  color: string
  checked: boolean
  onToggle: () => void
  onLedger: () => void
  ledgerOpen: boolean
  money: (v: number) => string
}) {
  const s = row.summary
  return (
    <TableRow>
      <TableCell>
        <input
          type="checkbox"
          aria-label={`Show ${row.name}`}
          checked={checked}
          onChange={onToggle}
          style={{ accentColor: color }}
        />
      </TableCell>
      <TableCell>
        <div className="font-medium">{row.name}</div>
        <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
          <Badge variant="outline">{KIND_LABEL[row.kind]}</Badge>
          {row.kind !== 'other' && <span>orders named {row.tags.join(', ')}</span>}
        </div>
      </TableCell>
      <TableCell className={cn('text-right font-medium', pnlColor(s.total_pnl))}>
        {money(s.total_pnl)}
      </TableCell>
      <TableCell className="text-right">
        {s.days} ({s.winning_days})
      </TableCell>
      <TableCell className="text-right whitespace-nowrap">
        {s.best_day == null ? '-' : `${money(s.best_day)} / ${money(s.worst_day ?? 0)}`}
      </TableCell>
      <TableCell className="text-right whitespace-nowrap">
        {money(s.max_drawdown)} ({formatPct(s.max_drawdown_pct)})
      </TableCell>
      <TableCell className="text-right">{s.trades}</TableCell>
      <TableCell className="whitespace-nowrap">{s.last_day ?? '-'}</TableCell>
      <TableCell>
        <Button size="sm" variant={ledgerOpen ? 'default' : 'outline'} onClick={onLedger}>
          Ledger
        </Button>
      </TableCell>
    </TableRow>
  )
}

function CombinedSummary({
  data,
  money,
}: {
  data: NonNullable<Awaited<ReturnType<typeof getDashboard>>['combined']>
  money: (v: number) => string
}) {
  const s = data.summary
  return (
    <div className="mt-4 grid gap-4 md:grid-cols-2">
      <div className="grid grid-cols-2 gap-3 text-sm">
        <div>
          <p className="text-muted-foreground">Combined P&L</p>
          <p className={cn('text-lg font-semibold', pnlColor(s.total_pnl))}>{money(s.total_pnl)}</p>
        </div>
        <div>
          <p className="text-muted-foreground">Max drawdown</p>
          <p className="text-lg font-semibold">
            {money(s.max_drawdown)} ({formatPct(s.max_drawdown_pct)})
          </p>
        </div>
        <div>
          <p className="text-muted-foreground">Days (won)</p>
          <p className="text-lg font-semibold">
            {s.days} ({s.winning_days})
          </p>
        </div>
        <div>
          <p className="text-muted-foreground">Trades</p>
          <p className="text-lg font-semibold">{s.trades}</p>
        </div>
      </div>
      <div className="text-sm">
        <p className="mb-1 text-muted-foreground">Each strategy's share</p>
        {data.contributions.map((c) => (
          <div key={c.key} className="flex justify-between border-b py-1">
            <span>{c.name}</span>
            <span className={pnlColor(c.total_pnl)}>{money(c.total_pnl)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function Ledger({
  strategyKey,
  name,
  money,
}: {
  strategyKey: string
  name: string
  money: (v: number) => string
}) {
  const fills = useQuery({
    queryKey: strategyDashboardKeys.ledger(strategyKey),
    queryFn: () => getLedger(strategyKey),
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>Ledger: {name}</CardTitle>
        <CardDescription>
          Every fill of its own orders, newest first, and its share of the Sandbox's end-of-day
          square-off.
        </CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Time</TableHead>
              <TableHead>Symbol</TableHead>
              <TableHead>Side</TableHead>
              <TableHead className="text-right">Quantity</TableHead>
              <TableHead className="text-right">Price</TableHead>
              <TableHead>Product</TableHead>
              <TableHead>Order name</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {(fills.data ?? []).map((f, i) => (
              <TableRow key={`${f.time}-${i}`}>
                <TableCell className="whitespace-nowrap">{f.time}</TableCell>
                <TableCell>
                  {f.symbol} <span className="text-xs text-muted-foreground">{f.exchange}</span>
                </TableCell>
                <TableCell>
                  {f.action}
                  {f.square_off && (
                    <span className="ml-1 text-xs text-muted-foreground">(square-off)</span>
                  )}
                </TableCell>
                <TableCell className="text-right">{f.quantity}</TableCell>
                <TableCell className="text-right">{money(f.price)}</TableCell>
                <TableCell>{f.product}</TableCell>
                <TableCell className="text-xs text-muted-foreground">{f.order_name}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {fills.isLoading && <p className="py-4 text-sm text-muted-foreground">Loading...</p>}
      </CardContent>
    </Card>
  )
}
