// pages/research/RunAnalytics.tsx
// Risk charts (drawdown, exposure) and what strategies did to each other when
// run together: netting, conflicts, competition for capital, overlap and
// correlation, plus any portfolio limit that stopped a day. Phase 6.

import {
  type EquityCurves,
  formatIstTime,
  formatNumber,
  formatPct,
  formatRupees,
  type LimitBreach,
  type TogetherMetrics,
} from '@/api/research'
import { PortfolioLineChart } from '@/components/portfolio/PortfolioLineChart'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

/** One point per IST day: the day's lowest or highest value of a series. */
function dailyExtreme(
  curves: EquityCurves,
  key: string,
  pick: 'min' | 'max'
): { date: string; value: number }[] {
  const out = new Map<string, number>()
  const values = curves.series[key] ?? []
  curves.timestamps.forEach((ts, i) => {
    const date = new Date(ts * 1000).toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' })
    const seen = out.get(date)
    const value = values[i]
    if (seen === undefined) out.set(date, value)
    else out.set(date, pick === 'min' ? Math.min(seen, value) : Math.max(seen, value))
  })
  return [...out.entries()].map(([date, value]) => ({ date, value }))
}

export function RiskCharts({ curves }: { curves: EquityCurves }) {
  const hasExposure = 'exposure_portfolio' in curves.series
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Drawdown</CardTitle>
          <CardDescription>
            How far the portfolio stood below its highest value, at the worst bar of each day.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <PortfolioLineChart
            height={220}
            format={(v) => formatPct(v)}
            series={[
              {
                name: 'Drawdown',
                color: '#ef4444',
                area: true,
                data: dailyExtreme(curves, 'drawdown_portfolio', 'min'),
              },
            ]}
          />
        </CardContent>
      </Card>
      {hasExposure && (
        <Card>
          <CardHeader>
            <CardTitle>Exposure</CardTitle>
            <CardDescription>
              The value of what the portfolio held, at the busiest bar of each day.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <PortfolioLineChart
              height={220}
              format={formatRupees}
              series={[
                {
                  name: 'Exposure',
                  color: '#3b82f6',
                  area: true,
                  data: dailyExtreme(curves, 'exposure_portfolio', 'max'),
                },
              ]}
            />
          </CardContent>
        </Card>
      )}
    </div>
  )
}

const BREACH_LABEL: Record<LimitBreach['reason'], string> = {
  daily_loss: 'Daily loss limit reached',
  daily_target: 'Daily profit target reached',
  lock_profit: 'Locked profit given back',
}

export function TogetherCard({
  together,
  breaches,
  labels,
  shared,
}: {
  together?: TogetherMetrics
  breaches?: LimitBreach[]
  labels: Map<number, string>
  shared: boolean
}) {
  if (!together && !breaches?.length) return null
  const name = (instance: number) => labels.get(instance) ?? `Strategy ${instance}`
  return (
    <Card>
      <CardHeader>
        <CardTitle>{together ? 'Strategies together' : 'Portfolio limits'}</CardTitle>
        <CardDescription>
          {together
            ? shared
              ? 'What sharing one portfolio did: orders it netted away, what that saved, how often the strategies disagreed and competed for capital, and how alike they traded.'
              : 'How alike the strategies traded. In isolated mode nothing is netted or shared.'
            : 'When a portfolio limit stopped a day.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {together && shared && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            <Fact label="Orders avoided" value={String(together.netting.orders_avoided)} />
            <Fact label="Value netted" value={formatRupees(together.netting.notional_netted)} />
            <Fact label="Costs saved" value={formatRupees(together.netting.costs_saved)} />
            <Fact label="Orders that crossed" value={formatPct(together.conflict_rate_pct, 1)} />
            <Fact label="Trimmed by the limit" value={String(together.reductions)} />
          </div>
        )}
        {together && together.overlap.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Strategies</TableHead>
                <TableHead className="text-right">Held the same side</TableHead>
                <TableHead className="text-right">Held opposite sides</TableHead>
                <TableHead className="text-right">Daily return correlation</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {together.overlap.map((pair) => {
                const corr = together.correlation.find((c) => c.a === pair.a && c.b === pair.b)
                return (
                  <TableRow key={`${pair.a}-${pair.b}`}>
                    <TableCell>
                      {name(pair.a)} and {name(pair.b)}
                    </TableCell>
                    <TableCell className="text-right">{formatPct(pair.same_pct, 1)}</TableCell>
                    <TableCell className="text-right">{formatPct(pair.opposite_pct, 1)}</TableCell>
                    <TableCell className="text-right">
                      {formatNumber(corr?.value ?? null)}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
        {breaches && breaches.length > 0 && (
          <ul className="space-y-1 text-sm">
            {breaches.map((b) => (
              <li key={`${b.portfolio}-${b.ts}`}>
                {formatIstTime(b.ts)}: {BREACH_LABEL[b.reason]}
                {shared ? '' : ` for ${name(b.portfolio)}`} at {formatRupees(b.day_pnl)} for the
                day. Positions were closed and no new trades were taken that day.
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
    </div>
  )
}
