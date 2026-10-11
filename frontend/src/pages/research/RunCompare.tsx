// pages/research/RunCompare.tsx
// Two runs of one scenario, decision by decision: built for a Shadow session
// beside the Sandbox or Live session of the same day (master PRD section 56).
// The same strategies saw the same bars, so where their orders or fill prices
// differ, the difference is the environment's.

import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import {
  compareRuns,
  ENVIRONMENT_LABEL,
  formatIstTime,
  formatNumber,
  formatRupees,
  researchQueryKeys,
  type SessionEnvironment,
} from '@/api/research'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

function where(environment: string): string {
  return ENVIRONMENT_LABEL[environment as SessionEnvironment] ?? 'Backtest'
}

function side(order: { net: number; fill_price: number | null } | null): string {
  if (!order) return 'No order'
  const action = order.net > 0 ? 'Buy' : 'Sell'
  const fill = order.fill_price === null ? 'not filled' : `at ${order.fill_price.toFixed(2)}`
  return `${action} ${Math.abs(order.net)} ${fill}`
}

export default function ResearchRunCompare() {
  const { first, second } = useParams<{ first: string; second: string }>()
  const a = Number(first)
  const b = Number(second)
  const { data, error, isLoading } = useQuery({
    queryKey: researchQueryKeys.compare(a, b),
    queryFn: () => compareRuns(a, b),
    enabled: Number.isFinite(a) && Number.isFinite(b),
  })

  if (error) {
    return (
      <p className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
        {(error as Error).message}
      </p>
    )
  }
  if (isLoading || !data) {
    return <p className="py-8 text-center text-sm text-muted-foreground">Loading…</p>
  }
  const [one, two] = data.runs

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">
          {where(one.environment)} against {where(two.environment)}
        </h1>
        <p className="text-sm text-muted-foreground">
          The same scenario's runs, decision by decision. Where the orders match, the difference in
          fill price is what the environment did.
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {data.runs.map((run) => (
          <Card key={run.id}>
            <CardHeader>
              <CardTitle>
                <Link to={`/research/runs/${run.id}`} className="hover:underline">
                  {where(run.environment)} #{run.id}
                </Link>
              </CardTitle>
              <CardDescription>
                {run.trading_date ?? ''} {run.status.toLowerCase()}
              </CardDescription>
            </CardHeader>
            <CardContent className="grid grid-cols-3 gap-3 text-sm">
              <div>
                <div className="text-xs text-muted-foreground">Net P&amp;L</div>
                {run.metrics ? formatRupees(run.metrics.net_pnl) : '-'}
              </div>
              <div>
                <div className="text-xs text-muted-foreground">Costs</div>
                {run.metrics ? formatRupees(run.metrics.costs) : '-'}
              </div>
              <div>
                <div className="text-xs text-muted-foreground">Trades</div>
                {run.metrics ? run.metrics.trades : '-'}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Orders</CardTitle>
          <CardDescription>
            {data.summary.same_decisions} decided alike, {data.summary.different_decisions}{' '}
            differently. Average fill price difference:{' '}
            {formatNumber(data.summary.average_price_difference)}.
          </CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Bar (IST)</TableHead>
                <TableHead>Symbol</TableHead>
                <TableHead>{where(one.environment)}</TableHead>
                <TableHead>{where(two.environment)}</TableHead>
                <TableHead className="text-right">Price difference</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.orders.map((row) => (
                <TableRow
                  key={`${row.bar_ts}-${row.portfolio}-${row.symbol}`}
                  className={row.same_decision ? '' : 'bg-destructive/5'}
                >
                  <TableCell className="whitespace-nowrap">{formatIstTime(row.bar_ts)}</TableCell>
                  <TableCell>{row.symbol}</TableCell>
                  <TableCell>{side(row.first)}</TableCell>
                  <TableCell>{side(row.second)}</TableCell>
                  <TableCell className="text-right">
                    {row.price_difference === null ? '' : row.price_difference.toFixed(2)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
