// pages/runs/PnlReport.tsx
// The P&L of whatever is asked for (Runs, one strategy within a Run, the main
// account, strategies outside Runs), from /runs/api/pnl: the headline figures,
// the charts, a calendar of days and every day as a table. Every figure is
// after charges and comes from the accounts, so it agrees with their balances.

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { formatPct } from '@/api/research'
import { getPnl, type PnlDay, type PnlQuery, type PnlSeries, runKeys } from '@/api/runs'
import { DailyPnlCalendar } from '@/components/portfolio/DailyPnlCalendar'
import { MonthlyReturnsHeatmap } from '@/components/portfolio/MonthlyReturnsHeatmap'
import { PortfolioLineChart, type SeriesSpec } from '@/components/portfolio/PortfolioLineChart'
import { Button } from '@/components/ui/button'
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
import { Kpi, tone, useMoney } from './shared'

export const COLORS = ['#3b82f6', '#c2410c', '#22c55e', '#8b5cf6', '#ef4444', '#14b8a6', '#ec4899']
const GAIN = '#16a34a'
const LOSS = '#dc2626'
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

export type View = 'individual' | 'combined'

export function hasScope(query: PnlQuery): boolean {
  return Boolean(
    query.runs?.length || query.members?.length || query.main || query.strategies?.length
  )
}

/** Every day of every series, as the table shows it and the CSV carries it. */
export interface DayRow {
  date: string
  name: string
  opening: number
  transfer: number
  pnl: number
  charges: number
  net: number
  trades: number
}

export function dayRows(series: PnlSeries[]): DayRow[] {
  return series
    .flatMap((s) =>
      s.daily.map((d) => ({
        date: d.date,
        name: s.name,
        opening: d.opening,
        transfer: d.transfer,
        pnl: d.pnl,
        charges: d.charges,
        net: d.net,
        trades: d.trades,
      }))
    )
    .sort((a, b) => b.date.localeCompare(a.date) || a.name.localeCompare(b.name))
}

export const CSV_HEADER = [
  'Day',
  'Of',
  'Opened with',
  'Moved at the open',
  'Gross',
  'Charges',
  'Net',
  'Trades',
]

export function toCsv(rows: DayRow[]): string {
  const cell = (v: string | number) => {
    const text = typeof v === 'number' ? v.toFixed(2).replace(/\.00$/, '') : v
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
  }
  return [
    CSV_HEADER.join(','),
    ...rows.map((r) =>
      [r.date, r.name, r.opening, r.transfer, r.pnl, r.charges, r.net, r.trades].map(cell).join(',')
    ),
  ].join('\n')
}

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv' }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  URL.revokeObjectURL(url)
}

/** Fall from the running best, in rupees, day by day. */
function drawdown(daily: PnlDay[]) {
  let best = 0
  return daily.map((d) => {
    best = Math.max(best, d.cumulative)
    return { date: d.date, value: d.cumulative - best }
  })
}

/** Each month's net as a share of what that month's first day opened with. */
function monthly(daily: PnlDay[]) {
  const months = new Map<string, { net: number; opening: number }>()
  for (const d of daily) {
    const key = d.date.slice(0, 7)
    const entry = months.get(key) ?? { net: 0, opening: d.opening }
    entry.net += d.net
    months.set(key, entry)
  }
  const years = [...new Set([...months.keys()].map((k) => k.slice(0, 4)))].sort()
  const values = years.map((y) =>
    MONTHS.map((_, i) => {
      const entry = months.get(`${y}-${String(i + 1).padStart(2, '0')}`)
      return entry?.opening ? entry.net / entry.opening : null
    })
  )
  return { years, values }
}

export function PnlReport({
  query,
  view,
  title = 'Cumulative net P&L',
}: {
  query: PnlQuery
  view: View
  title?: string
}) {
  const money = useMoney()
  const data = useQuery({
    queryKey: runKeys.pnl(query),
    queryFn: () => getPnl(query),
    enabled: hasScope(query),
    refetchInterval: 60_000,
  })
  const [selected, setSelected] = useState<string | null>(null)
  const series = data.data?.series ?? []
  const combined = data.data?.combined
  const daily = combined?.daily ?? []
  const s = combined?.summary
  const rows = useMemo(() => dayRows(series), [series])
  const colorOf = (key: string) => COLORS[series.findIndex((x) => x.key === key) % COLORS.length]

  if (!hasScope(query)) {
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        Choose a Run, the main account or the strategies outside Runs above.
      </p>
    )
  }
  if (data.error) {
    return (
      <p className="text-sm text-destructive">
        The P&L could not be worked out just now. Refresh the page to try again.
      </p>
    )
  }
  if (data.isLoading || !combined) {
    return <p className="py-10 text-center text-sm text-muted-foreground">Loading...</p>
  }
  if (!daily.length) {
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        No trading days in this window for what is chosen.
      </p>
    )
  }

  const last = daily[daily.length - 1]
  const lines: SeriesSpec[] =
    view === 'combined' || series.length === 1
      ? [
          {
            name: view === 'combined' && series.length > 1 ? 'Combined' : series[0].name,
            color: COLORS[0],
            area: true,
            data: daily.map((d) => ({ date: d.date, value: d.cumulative })),
          },
        ]
      : series.map((x) => ({
          name: x.name,
          color: colorOf(x.key),
          data: x.daily.map((d) => ({ date: d.date, value: d.cumulative })),
        }))
  const bars: SeriesSpec[] = [
    {
      name: 'Net',
      color: GAIN,
      histogram: true,
      data: daily.map((d) => ({ date: d.date, value: d.net, color: d.net >= 0 ? GAIN : LOSS })),
    },
  ]
  const fall: SeriesSpec[] = [{ name: 'Below its best', color: LOSS, data: drawdown(daily) }]
  const months = monthly(daily)
  const widest = Math.max(...series.map((x) => Math.abs(x.summary.total_pnl)), 1)
  const picked = selected
    ? series.map((x) => ({ x, d: x.daily.find((d) => d.date === selected) }))
    : []
  const pickedDay = selected ? daily.find((d) => d.date === selected) : undefined

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi
          label="Net, after charges"
          value={money(s?.total_pnl ?? 0)}
          className={tone(s?.total_pnl)}
        />
        <Kpi
          label="Return on opening capital"
          value={formatPct(s?.return_pct)}
          className={tone(s?.return_pct)}
        />
        <Kpi
          label="Largest drawdown"
          value={`${money(s?.max_drawdown ?? 0)} · ${formatPct(s?.max_drawdown_pct)}`}
        />
        <Kpi label="Days won" value={`${s?.winning_days ?? 0} of ${s?.days ?? 0}`} />
        <Kpi label="Charges" value={money(s?.charges ?? 0)} />
        <Kpi
          label="Best and worst day"
          value={`${money(s?.best_day ?? 0)} / ${money(s?.worst_day ?? 0)}`}
        />
        <Kpi label="Trades" value={s?.trades ?? 0} />
        <Kpi
          label="Capital now"
          value={money(last.opening + last.net)}
          hint={`after ${last.date}`}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>{title}</CardTitle>
            <CardDescription>
              {view === 'combined' && series.length > 1
                ? 'Everything chosen, added up day by day.'
                : 'Each one on its own, its net added up day by day.'}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <PortfolioLineChart series={lines} format={money} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Each day's net</CardTitle>
            <CardDescription>A gain in green, a loss in red.</CardDescription>
          </CardHeader>
          <CardContent>
            <PortfolioLineChart series={bars} format={money} height={260} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Drawdown</CardTitle>
            <CardDescription>How far below its best it has been, in rupees.</CardDescription>
          </CardHeader>
          <CardContent>
            <PortfolioLineChart series={fall} format={money} height={220} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Who made it</CardTitle>
            <CardDescription>Each one's share of the window's net.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {series.map((x) => (
              <div key={x.key} className="space-y-1 text-sm">
                <div className="flex justify-between gap-2">
                  <span className="truncate">{x.name}</span>
                  <span className={cn('tabular-nums', tone(x.summary.total_pnl))}>
                    {money(x.summary.total_pnl)}
                  </span>
                </div>
                <div className="h-2 rounded bg-muted" aria-hidden="true">
                  <div
                    className="h-2 rounded"
                    style={{
                      width: `${(Math.abs(x.summary.total_pnl) / widest) * 100}%`,
                      background: x.summary.total_pnl >= 0 ? GAIN : LOSS,
                    }}
                  />
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>By month</CardTitle>
            <CardDescription>Each month's net, % of what it opened with.</CardDescription>
          </CardHeader>
          <CardContent>
            <MonthlyReturnsHeatmap years={months.years} columns={MONTHS} values={months.values} />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Calendar</CardTitle>
          <CardDescription>Choose a day to see who made what on it.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-6 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <DailyPnlCalendar
              days={daily}
              format={money}
              selected={selected}
              onSelect={setSelected}
            />
          </div>
          <aside className="rounded-lg border bg-muted/30 p-4" aria-label="The day chosen">
            {pickedDay ? (
              <div className="space-y-3 text-sm">
                <h3 className="font-semibold">{selected}</h3>
                <div className="flex justify-between">
                  <span>Net, after charges</span>
                  <span className={cn('tabular-nums', tone(pickedDay.net))}>
                    {money(pickedDay.net)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span>Moved at the open</span>
                  <span className="tabular-nums">{money(pickedDay.transfer)}</span>
                </div>
                <div className="flex justify-between">
                  <span>Trades</span>
                  <span className="tabular-nums">{pickedDay.trades}</span>
                </div>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Of</TableHead>
                      <TableHead className="text-right">Opened</TableHead>
                      <TableHead className="text-right">Charges</TableHead>
                      <TableHead className="text-right">Net</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {picked.map(({ x, d }) => (
                      <TableRow key={x.key}>
                        <TableCell>{x.name}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {d ? money(d.opening) : '-'}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {d ? money(d.charges) : '-'}
                        </TableCell>
                        <TableCell className={cn('text-right tabular-nums', tone(d?.net))}>
                          {d ? money(d.net) : '-'}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                Choose a day in the calendar to see its figures here.
              </p>
            )}
          </aside>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle>Every day</CardTitle>
            <CardDescription>Newest first. The CSV holds exactly these rows.</CardDescription>
          </div>
          <Button variant="outline" onClick={() => download('pnl.csv', toCsv(rows))}>
            Download CSV
          </Button>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table aria-label="Every day">
            <TableHeader>
              <TableRow>
                {CSV_HEADER.map((h, i) => (
                  <TableHead key={h} className={cn(i > 1 && 'text-right')}>
                    {h}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={`${r.date}-${r.name}`}>
                  <TableCell className="whitespace-nowrap">{r.date}</TableCell>
                  <TableCell>{r.name}</TableCell>
                  <TableCell className="text-right tabular-nums">{money(r.opening)}</TableCell>
                  <TableCell className="text-right tabular-nums">{money(r.transfer)}</TableCell>
                  <TableCell className={cn('text-right tabular-nums', tone(r.pnl))}>
                    {money(r.pnl)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{money(r.charges)}</TableCell>
                  <TableCell className={cn('text-right tabular-nums', tone(r.net))}>
                    {money(r.net)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{r.trades}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
