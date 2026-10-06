// pages/research/RunDetail.tsx
// One backtest run: what it earned, who earned it, every round trip, and the
// exact inputs that produced it. A run never changes after it completes, so
// this page always shows what produced the result, even after the strategy or
// its configuration has moved on.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  ACTIVE_STATUSES,
  cancelRun,
  type EquityCurves,
  formatIstTime,
  formatNumber,
  formatPct,
  formatRupees,
  getRun,
  getRunEquity,
  getRunTrades,
  type RunDetail,
  type RunMetrics,
  researchQueryKeys,
  shortHash,
} from '@/api/research'
import { PortfolioLineChart } from '@/components/portfolio/PortfolioLineChart'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { showToast } from '@/utils/toast'
import { StatusBadge } from './ExperimentDetail'

const COLORS = ['#3b82f6', '#22c55e', '#f59e0b', '#8b5cf6', '#ef4444', '#14b8a6', '#ec4899']

/** Last equity value of each IST day, as the daily points the chart plots. */
function daily(curves: EquityCurves, key: string): { date: string; value: number }[] {
  const out = new Map<string, number>()
  const values = curves.series[key] ?? []
  curves.timestamps.forEach((ts, i) => {
    const date = new Date(ts * 1000).toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' })
    out.set(date, values[i])
  })
  return [...out.entries()].map(([date, value]) => ({ date, value }))
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
    </div>
  )
}

function MetricGrid({ m }: { m: RunMetrics }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
      <Stat label="Capital" value={formatRupees(m.capital)} />
      <Stat label="Net P&L" value={formatRupees(m.net_pnl)} />
      <Stat label="Return" value={formatPct(m.return_pct)} />
      <Stat label="Annual return" value={formatPct(m.cagr_pct)} />
      <Stat label="Max drawdown" value={formatPct(m.max_drawdown_pct)} />
      <Stat label="Sharpe" value={formatNumber(m.sharpe)} />
      <Stat label="Sortino" value={formatNumber(m.sortino)} />
      <Stat label="Trades" value={String(m.trades)} />
      <Stat label="Win rate" value={formatPct(m.win_rate, 1)} />
      <Stat label="Profit factor" value={formatNumber(m.profit_factor)} />
      <Stat label="Average trade" value={m.avg_trade == null ? 'n/a' : formatRupees(m.avg_trade)} />
      <Stat label="Costs" value={formatRupees(m.costs)} />
    </div>
  )
}

export default function ResearchRunDetail() {
  const { runId } = useParams<{ runId: string }>()
  const id = Number(runId)
  const queryClient = useQueryClient()
  const [instance, setInstance] = useState<number | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: researchQueryKeys.run(id),
    queryFn: () => getRun(id),
    enabled: Number.isFinite(id),
    refetchInterval: (query) =>
      query.state.data && ACTIVE_STATUSES.includes(query.state.data.status) ? 2000 : false,
  })
  const completed = data?.status === 'COMPLETED'
  const equity = useQuery({
    queryKey: researchQueryKeys.equity(id),
    queryFn: () => getRunEquity(id),
    enabled: completed,
  })
  const trades = useQuery({
    queryKey: researchQueryKeys.trades(id, instance),
    queryFn: () => getRunTrades(id, instance),
    enabled: completed,
  })
  const cancel = useMutation({
    mutationFn: () => cancelRun(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: researchQueryKeys.all }),
    onError: (err: Error) => showToast.error(err.message),
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

  const labels = new Map((data.attribution ?? []).map((row) => [row.instance, row.label] as const))

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <Link
            to={`/research/experiments/${data.scenario.experiment_id}`}
            className="text-sm text-muted-foreground hover:underline"
          >
            Experiment
          </Link>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
            Run #{data.id}: {data.scenario.name} <StatusBadge status={data.status} />
          </h1>
          {data.message && <p className="text-sm text-muted-foreground">{data.message}</p>}
        </div>
        {ACTIVE_STATUSES.includes(data.status) && (
          <Button variant="outline" onClick={() => cancel.mutate()}>
            Cancel
          </Button>
        )}
      </div>

      {ACTIVE_STATUSES.includes(data.status) && (
        <Progress value={Math.round(data.progress * 100)} />
      )}

      {data.metrics && (
        <Card>
          <CardHeader>
            <CardTitle>
              {data.scenario.mode === 'SHARED' ? 'Shared portfolio' : 'All strategies'}
            </CardTitle>
            <CardDescription>
              Costs include brokerage, taxes and the scenario's slippage. Annual return is shown
              only for windows of a year or more.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <MetricGrid m={data.metrics} />
          </CardContent>
        </Card>
      )}

      {equity.data && (
        <Card>
          <CardHeader>
            <CardTitle>Equity</CardTitle>
            <CardDescription>End-of-day value. Drawdown above uses every bar.</CardDescription>
          </CardHeader>
          <CardContent>
            <PortfolioLineChart
              format={formatRupees}
              series={[
                {
                  name: data.scenario.mode === 'SHARED' ? 'Portfolio' : 'All strategies',
                  color: COLORS[0],
                  data: daily(equity.data, 'portfolio'),
                },
                ...(data.attribution ?? []).map((row, n) => ({
                  name: row.label,
                  color: COLORS[(n + 1) % COLORS.length],
                  data: daily(equity.data, `instance_${row.instance}`),
                })),
              ]}
            />
          </CardContent>
        </Card>
      )}

      {data.attribution && (
        <Card>
          <CardHeader>
            <CardTitle>By strategy</CardTitle>
            <CardDescription>
              Each strategy's own book: its trades, its costs and its share of the result.
              {data.scenario.mode === 'SHARED' &&
                ` Orders between strategies that cancelled out: ${data.metrics?.conflicts ?? 0}.`}
            </CardDescription>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Strategy</TableHead>
                  <TableHead className="text-right">Capital</TableHead>
                  <TableHead className="text-right">Gross</TableHead>
                  <TableHead className="text-right">Costs</TableHead>
                  <TableHead className="text-right">Net</TableHead>
                  <TableHead className="text-right">Return</TableHead>
                  <TableHead className="text-right">Max DD</TableHead>
                  <TableHead className="text-right">Trades</TableHead>
                  <TableHead className="text-right">Won / lost</TableHead>
                  <TableHead className="text-right">Trimmed by limit</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.attribution.map((row) => (
                  <TableRow key={row.instance}>
                    <TableCell className="font-medium">{row.label}</TableCell>
                    <TableCell className="text-right">{formatRupees(row.capital)}</TableCell>
                    <TableCell className="text-right">{formatRupees(row.gross_pnl)}</TableCell>
                    <TableCell className="text-right">{formatRupees(row.costs)}</TableCell>
                    <TableCell className="text-right">{formatRupees(row.net_pnl)}</TableCell>
                    <TableCell className="text-right">{formatPct(row.return_pct)}</TableCell>
                    <TableCell className="text-right">{formatPct(row.max_drawdown_pct)}</TableCell>
                    <TableCell className="text-right">{row.trades}</TableCell>
                    <TableCell className="text-right">
                      {row.winning_trades} / {row.losing_trades}
                    </TableCell>
                    <TableCell className="text-right">{row.reduced_by_limit}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {completed && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0">
            <div>
              <CardTitle>Trades</CardTitle>
              <CardDescription>
                Round trips, flat to flat.{' '}
                {trades.data && trades.data.total > trades.data.items.length
                  ? `Showing the first ${trades.data.items.length} of ${trades.data.total}.`
                  : ''}
              </CardDescription>
            </div>
            <div className="flex gap-1">
              <Button
                size="sm"
                variant={instance === null ? 'default' : 'outline'}
                onClick={() => setInstance(null)}
              >
                All
              </Button>
              {(data.attribution ?? []).map((row) => (
                <Button
                  key={row.instance}
                  size="sm"
                  variant={instance === row.instance ? 'default' : 'outline'}
                  onClick={() => setInstance(row.instance)}
                >
                  {row.label}
                </Button>
              ))}
            </div>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Strategy</TableHead>
                  <TableHead>Symbol</TableHead>
                  <TableHead>Side</TableHead>
                  <TableHead className="text-right">Qty</TableHead>
                  <TableHead>Entry</TableHead>
                  <TableHead className="text-right">Entry price</TableHead>
                  <TableHead>Exit</TableHead>
                  <TableHead className="text-right">Exit price</TableHead>
                  <TableHead className="text-right">Gross</TableHead>
                  <TableHead className="text-right">Costs</TableHead>
                  <TableHead className="text-right">Net</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(trades.data?.items ?? []).map((t) => (
                  <TableRow key={t.seq}>
                    <TableCell>{labels.get(t.instance) ?? t.instance}</TableCell>
                    <TableCell>{t.symbol}</TableCell>
                    <TableCell>{t.side === 'LONG' ? 'Long' : 'Short'}</TableCell>
                    <TableCell className="text-right">{t.qty}</TableCell>
                    <TableCell>{formatIstTime(t.entry_ts)}</TableCell>
                    <TableCell className="text-right">{t.entry_price.toFixed(2)}</TableCell>
                    <TableCell>{formatIstTime(t.exit_ts)}</TableCell>
                    <TableCell className="text-right">{t.exit_price.toFixed(2)}</TableCell>
                    <TableCell className="text-right">{formatRupees(t.gross_pnl)}</TableCell>
                    <TableCell className="text-right">
                      {formatRupees(t.fees + t.slippage)}
                    </TableCell>
                    <TableCell className="text-right">{formatRupees(t.net_pnl)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      <Provenance run={data} />
    </div>
  )
}

function Provenance({ run }: { run: RunDetail }) {
  const scenario = run.scenario
  const rows: [string, string][] = [
    ['Run identity', run.run_hash ?? 'Assigned when the run starts'],
    ['Engine', `v${run.engine_version ?? '-'} / ${shortHash(run.engine_hash)}`],
    [
      'Market data',
      run.dataset ? `${run.dataset.dataset_hash} (${run.dataset.bar_count} bars)` : '-',
    ],
    [
      'Data window',
      `${scenario.dataset_spec.interval} bars, ${scenario.dataset_spec.start} to ${scenario.dataset_spec.end}, ${scenario.dataset_spec.exchange}`,
    ],
    ['Mode', scenario.mode === 'SHARED' ? 'Shared portfolio' : 'Isolated'],
    [
      'Costs',
      `${scenario.cost_model.preset}, slippage ${scenario.cost_model.slippage_bps} bps, fills at next bar open`,
    ],
  ]
  return (
    <Card>
      <CardHeader>
        <CardTitle>What produced this result</CardTitle>
        <CardDescription>
          The exact versions this run used. They never change, so this result can be reproduced.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <dl className="grid gap-x-6 gap-y-1 sm:grid-cols-[auto_1fr]">
          {rows.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-muted-foreground">{k}</dt>
              <dd className="break-all font-mono text-xs">{v}</dd>
            </div>
          ))}
        </dl>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Strategy</TableHead>
              <TableHead>Version</TableHead>
              <TableHead>Model</TableHead>
              <TableHead>Configuration</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {scenario.instances.map((inst) => (
              <TableRow key={inst.position}>
                <TableCell>
                  <Link to={`/research/strategies/${inst.strategy_id}`} className="hover:underline">
                    {inst.strategy_key}
                  </Link>
                </TableCell>
                <TableCell className="font-mono text-xs">
                  {inst.strategy_version.dist_version} /{' '}
                  {shortHash(inst.strategy_version.version_hash)}
                </TableCell>
                <TableCell className="font-mono text-xs">
                  {shortHash(inst.strategy_version.artifact_hash)}
                </TableCell>
                <TableCell>
                  v{inst.config_version.version_no}:{' '}
                  {formatRupees(inst.config_version.config.capital)},{' '}
                  {inst.config_version.config.leverage}x, {inst.config_version.config.product},{' '}
                  {inst.config_version.config.entry_time}-
                  {inst.config_version.config.exit_time ?? 'hold'}{' '}
                  <span className="font-mono text-xs text-muted-foreground">
                    {shortHash(inst.config_version.config_hash)}
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}
