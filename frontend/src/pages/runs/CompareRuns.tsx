// pages/runs/CompareRuns.tsx
// Runs side by side over the days they all traded, each as a share of what it
// opened with so different capitals compare fairly; who made each one's
// result; and a backtest of one beside its Sandbox days.

import { useQuery } from '@tanstack/react-query'
import { type ReactNode, useState } from 'react'
import { useSearchParams } from 'react-router'
import { formatPct } from '@/api/research'
import { getPnl, listRuns, type PnlDay, type PnlQuery, type PnlSeries, runKeys } from '@/api/runs'
import { PortfolioLineChart, type SeriesSpec } from '@/components/portfolio/PortfolioLineChart'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { BacktestPanel } from './BacktestPanel'
import { COLORS } from './PnlReport'
import { RunsNav, tone, useMoney } from './shared'

/** The days every series traded, oldest first. */
export function commonDays(series: PnlSeries[]): string[] {
  if (!series.length) return []
  const [first, ...rest] = series.map((s) => new Set(s.daily.map((d) => d.date)))
  return [...first].filter((d) => rest.every((set) => set.has(d))).sort()
}

/** One series over the aligned days: its return and drawdown, as % of what it opened with. */
export function aligned(series: PnlSeries, days: string[]) {
  const byDate = new Map(series.daily.map((d) => [d.date, d]))
  const rows = days.map((d) => byDate.get(d)).filter((d): d is PnlDay => Boolean(d))
  const base = rows[0]?.opening ?? 0
  let total = 0
  let peak = base
  let worst = 0
  const returns: { date: string; value: number }[] = []
  const falls: { date: string; value: number }[] = []
  for (const d of rows) {
    total += d.net
    const equity = base + total
    peak = Math.max(peak, equity)
    const fall = peak ? ((equity - peak) / peak) * 100 : 0
    worst = Math.min(worst, fall)
    returns.push({ date: d.date, value: base ? (total / base) * 100 : 0 })
    falls.push({ date: d.date, value: fall })
  }
  const nets = rows.map((d) => d.net)
  return {
    returns,
    falls,
    net: total,
    returnPct: base ? (total / base) * 100 : null,
    drawdownPct: worst,
    won: nets.filter((n) => n > 0).length,
    days: rows.length,
    best: nets.length ? Math.max(...nets) : null,
    worst: nets.length ? Math.min(...nets) : null,
    charges: rows.reduce((t, d) => t + d.charges, 0),
    trades: rows.reduce((t, d) => t + d.trades, 0),
  }
}

export default function CompareRuns() {
  const money = useMoney()
  const [params, setParams] = useSearchParams()
  const runs = useQuery({ queryKey: runKeys.list(), queryFn: listRuns })
  const traded = (runs.data ?? []).filter((r) => r.status !== 'draft')
  const chosen = (params.get('runs') ?? '')
    .split(',')
    .map(Number)
    .filter((id) => traded.some((r) => r.id === id))
  const [backtestRun, setBacktestRun] = useState<number | null>(null)

  const pnl = useQuery({
    queryKey: runKeys.pnl({ runs: chosen }),
    queryFn: () => getPnl({ runs: chosen }),
    enabled: chosen.length > 0,
  })
  const series = pnl.data?.series ?? []
  const days = commonDays(series)
  // Each chosen Run's strategies over the same days, for who made what.
  const memberQuery: PnlQuery = {
    members: traded
      .filter((r) => chosen.includes(r.id))
      .flatMap((r) => r.version.members.map((m) => [r.id, m.seat] as [number, string])),
    from: days[0],
    to: days[days.length - 1],
  }
  const members = useQuery({
    queryKey: runKeys.pnl(memberQuery),
    queryFn: () => getPnl(memberQuery),
    enabled: days.length > 0,
  })

  const setChosen = (ids: number[]) => setParams(ids.length ? { runs: ids.join(',') } : {})
  const toggle = (id: number) =>
    setChosen(chosen.includes(id) ? chosen.filter((i) => i !== id) : [...chosen, id])
  const figures = series.map((s) => ({ s, f: aligned(s, days) }))
  const returnLines: SeriesSpec[] = figures.map(({ s, f }, i) => ({
    name: s.name,
    color: COLORS[i % COLORS.length],
    data: f.returns,
  }))
  const fallLines: SeriesSpec[] = figures.map(({ s, f }, i) => ({
    name: s.name,
    color: COLORS[i % COLORS.length],
    data: f.falls,
  }))
  const pct = (v: number) => `${v.toFixed(2)}%`
  const backtesting =
    traded.find((r) => r.id === backtestRun) ?? traded.find((r) => r.id === chosen[0])

  return (
    <div className="container mx-auto space-y-6 px-4 py-6">
      <RunsNav />
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Compare Runs</h1>
        <p className="text-muted-foreground">
          Over the days they all traded, as a share of what each opened with, so different capitals
          compare fairly.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {traded.map((r, i) => (
          <button
            key={r.id}
            type="button"
            aria-pressed={chosen.includes(r.id)}
            onClick={() => toggle(r.id)}
            className={cn(
              'rounded-full border px-3 py-1.5 text-sm',
              chosen.includes(r.id)
                ? 'border-primary bg-primary/10 font-medium'
                : 'border-border text-muted-foreground'
            )}
          >
            <span
              aria-hidden="true"
              className="mr-1.5 inline-block h-2 w-2 rounded-sm"
              style={{
                background:
                  COLORS[
                    chosen.indexOf(r.id) >= 0
                      ? chosen.indexOf(r.id) % COLORS.length
                      : i % COLORS.length
                  ],
              }}
            />
            {r.name}
          </button>
        ))}
        {days.length > 0 && (
          <span className="text-sm text-muted-foreground">
            {days.length} common days, {days[0]} to {days[days.length - 1]}
          </span>
        )}
      </div>

      {chosen.length === 0 ? (
        <p className="py-10 text-center text-sm text-muted-foreground">
          {traded.length
            ? 'Choose the Runs to compare above.'
            : 'No Run has traded yet, so there is nothing to compare.'}
        </p>
      ) : series.length > 0 && days.length === 0 ? (
        <p className="py-10 text-center text-sm text-muted-foreground">
          These Runs have no trading day in common yet.
        </p>
      ) : (
        days.length > 0 && (
          <>
            <div className="grid gap-4 lg:grid-cols-3">
              <Card className="lg:col-span-2">
                <CardHeader>
                  <CardTitle>Return on opening capital</CardTitle>
                </CardHeader>
                <CardContent>
                  <PortfolioLineChart series={returnLines} format={pct} />
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle>Drawdown</CardTitle>
                  <CardDescription>Each one's fall from its best, as a percentage.</CardDescription>
                </CardHeader>
                <CardContent>
                  <PortfolioLineChart series={fallLines} format={pct} height={260} />
                </CardContent>
              </Card>
            </div>

            <div className="grid gap-4 lg:grid-cols-3">
              <Card className="lg:col-span-2">
                <CardHeader>
                  <CardTitle>Side by side</CardTitle>
                </CardHeader>
                <CardContent className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Measure</TableHead>
                        {figures.map(({ s }) => (
                          <TableHead key={s.key} className="text-right">
                            {s.name}
                          </TableHead>
                        ))}
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {(
                        [
                          [
                            'Net, after charges',
                            (f) => <span className={tone(f.net)}>{money(f.net)}</span>,
                          ],
                          ['Return on opening capital', (f) => formatPct(f.returnPct)],
                          ['Largest drawdown', (f) => formatPct(f.drawdownPct)],
                          ['Days won', (f) => `${f.won} of ${f.days}`],
                          [
                            'Best and worst day',
                            (f) => `${money(f.best ?? 0)} / ${money(f.worst ?? 0)}`,
                          ],
                          ['Charges', (f) => money(f.charges)],
                          ['Trades', (f) => f.trades],
                        ] as [string, (f: ReturnType<typeof aligned>) => ReactNode][]
                      ).map(([label, cell]) => (
                        <TableRow key={label}>
                          <TableCell>{label}</TableCell>
                          {figures.map(({ s, f }) => (
                            <TableCell key={s.key} className="text-right tabular-nums">
                              {cell(f)}
                            </TableCell>
                          ))}
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle>Who made it</CardTitle>
                  <CardDescription>
                    Each strategy's share in each Run. The same strategy can differ between Runs:
                    the policy moves its capital.
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4 text-sm">
                  {traded
                    .filter((r) => chosen.includes(r.id))
                    .map((r) => (
                      <div key={r.id}>
                        <p className="mb-1 font-medium">{r.name}</p>
                        {r.version.members.map((m) => {
                          const own = members.data?.series.find(
                            (x) => x.key === `member:${r.id}:${m.seat}`
                          )
                          const value = own?.summary.total_pnl
                          return (
                            <div key={m.seat} className="flex justify-between border-b py-1">
                              <span>{m.label}</span>
                              <span className={cn('tabular-nums', tone(value))}>
                                {value == null ? '-' : money(value)}
                              </span>
                            </div>
                          )
                        })}
                      </div>
                    ))}
                </CardContent>
              </Card>
            </div>
          </>
        )
      )}

      {traded.length > 0 && backtesting && (
        <div className="space-y-2">
          {traded.length > 1 && (
            <label className="flex items-center gap-2 text-sm">
              Backtest
              <select
                value={backtesting.id}
                onChange={(e) => setBacktestRun(Number(e.target.value))}
                className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
              >
                {traded.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <BacktestPanel key={backtesting.id} run={backtesting} />
        </div>
      )}
    </div>
  )
}
