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
  ENVIRONMENT_LABEL,
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
  type RunProvenanceNotes,
  researchQueryKeys,
  type SessionEnvironment,
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
import { RiskCharts, TogetherCard } from './RunAnalytics'
import { Reconciliation, RunLedger } from './RunLedger'
import { RunLog } from './RunLog'

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
      {m.final_equity != null && (
        <Stat label="Final capital" value={formatRupees(m.final_equity)} />
      )}
      {m.avg_exposure_pct != null && (
        <Stat label="Average exposure" value={formatPct(m.avg_exposure_pct, 1)} />
      )}
      {m.capital_used_pct != null && (
        <Stat label="Buying power used" value={formatPct(m.capital_used_pct, 1)} />
      )}
      {m.time_in_market_pct != null && (
        <Stat label="Time in the market" value={formatPct(m.time_in_market_pct, 1)} />
      )}
    </div>
  )
}

function ranFor(start: string, end: string): string {
  const seconds = Math.max(0, Math.round((Date.parse(end) - Date.parse(start)) / 1000))
  if (seconds < 60) return `${seconds} s`
  const minutes = Math.floor(seconds / 60)
  return minutes < 60
    ? `${minutes} min ${seconds % 60} s`
    : `${Math.floor(minutes / 60)} h ${minutes % 60} min`
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

  const labels = new Map<number, string>(
    data.attribution
      ? data.attribution.map((row) => [row.instance, row.label] as const)
      : data.scenario.instances.map(
          (inst) =>
            [
              inst.position,
              `${inst.strategy_key} (config v${inst.config_version.version_no})`,
            ] as const
        )
  )

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
          {data.started_at && data.finished_at && (
            <p className="text-sm text-muted-foreground">
              Ran for {ranFor(data.started_at, data.finished_at)}.
            </p>
          )}
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

      {data.in_sample?.length > 0 && (
        <div className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
          <p className="font-medium">
            In-sample: this backtest covers data a strategy was trained on
          </p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {data.in_sample.map((row) => (
              <li key={row.position}>
                {row.strategy_key} was trained on {row.training_start} to {row.training_end};{' '}
                {row.all_of_it
                  ? 'this whole backtest falls inside that period.'
                  : `this backtest overlaps it from ${row.overlap_start} to ${row.overlap_end}.`}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-muted-foreground">
            Results over the training period overstate how a strategy trades data it has not seen.
            Judge it on a window after{' '}
            {data.in_sample
              .map((row) => row.training_end)
              .sort()
              .at(-1)}
            .
          </p>
        </div>
      )}

      {data.provenance?.replay && <ReplayNotice replay={data.provenance.replay} />}

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

      {equity.data && 'drawdown_portfolio' in equity.data.series && (
        <RiskCharts curves={equity.data} />
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
                  <TableHead className="text-right">Share</TableHead>
                  <TableHead className="text-right">Return</TableHead>
                  <TableHead className="text-right">Max DD</TableHead>
                  <TableHead className="text-right">Avg exposure</TableHead>
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
                    <TableCell className="text-right">
                      {formatPct(row.contribution_pct ?? null, 1)}
                    </TableCell>
                    <TableCell className="text-right">{formatPct(row.return_pct)}</TableCell>
                    <TableCell className="text-right">{formatPct(row.max_drawdown_pct)}</TableCell>
                    <TableCell className="text-right">
                      {formatPct(row.avg_exposure_pct ?? null, 1)}
                    </TableCell>
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

      {data.metrics && (
        <TogetherCard
          together={data.metrics.together}
          breaches={data.metrics.limit_breaches}
          labels={labels}
          shared={data.scenario.mode === 'SHARED'}
        />
      )}

      {data.status !== 'QUEUED' && (
        <>
          <RunLedger runId={id} labels={labels} />
          <Reconciliation runId={id} live={ACTIVE_STATUSES.includes(data.status)} />
        </>
      )}

      <RunLog runId={id} live={ACTIVE_STATUSES.includes(data.status)} />

      <Provenance run={data} />
    </div>
  )
}

/** How a Python strategy's replayed days went, when any did not end cleanly. */
export function ReplayNotice({ replay }: { replay: NonNullable<RunProvenanceNotes['replay']> }) {
  const asked = Object.entries(replay.unsupported ?? {})
  if (replay.stopped_early.length === 0 && asked.length === 0) return null
  return (
    <div className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
      {replay.stopped_early.length > 0 && (
        <>
          <p className="font-medium">
            The script did not finish {replay.stopped_early.length} of its {replay.days} days
          </p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {replay.stopped_early.map((day) => (
              <li key={`${day.day}-${day.at}`}>
                {day.day}
                {day.at ? ` at ${day.at}` : ''}: {day.message}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-muted-foreground">
            The run log has the script's own output for each day.
          </p>
        </>
      )}
      {asked.length > 0 && (
        <p className={replay.stopped_early.length > 0 ? 'mt-2' : ''}>
          It asked for what a backtest cannot provide, and was refused:{' '}
          {asked.map(([endpoint, count]) => `${endpoint} (${count} times)`).join(', ')}.
        </p>
      )}
    </div>
  )
}

function Provenance({ run }: { run: RunDetail }) {
  const scenario = run.scenario
  // Any session (Sandbox, Shadow, Live) traded live market data: it is a record,
  // not a result that a rerun could reproduce.
  const sandbox = run.environment !== undefined && run.environment !== 'BACKTEST'
  const hosted = run.environment === 'HOSTED'
  const fills = {
    SANDBOX: 'sandbox fills',
    SHADOW: 'fills simulated at the latest tick, nothing sent',
    LIVE: 'real broker fills',
    HOSTED: `the sandbox fills of its own orders (${run.provenance?.recorded_from ?? 'tagged'})`,
  }[run.environment as SessionEnvironment | 'HOSTED']
  const rows: [string, string][] = [
    [
      'Run identity',
      sandbox
        ? `${ENVIRONMENT_LABEL[run.environment as SessionEnvironment | 'HOSTED']} session on ${run.trading_date}: live market data and ${fills}, so it is a record rather than a reproducible result`
        : (run.run_hash ?? 'Assigned when the run starts'),
    ],
    ['Engine', `v${run.engine_version ?? '-'} / ${shortHash(run.engine_hash)}`],
    [
      'Market data',
      hosted
        ? run.provenance?.marks === 'historify'
          ? 'Positions marked on stored 1m bars from Historify for the symbols it traded'
          : 'Positions marked at its own fill prices: no stored bars for that day'
        : sandbox
          ? `Live ${scenario.dataset_spec.interval} bars built from ticks, ${scenario.dataset_spec.exchange}; every bar the session saw is stored with it`
          : run.dataset
            ? `${run.dataset.dataset_hash} (${run.dataset.bar_count} bars${
                run.dataset.symbol_count ? ` of ${run.dataset.symbol_count} symbols` : ''
              }${
                run.dataset.first_ts && run.dataset.last_ts
                  ? `, ${formatIstTime(run.dataset.first_ts)} to ${formatIstTime(run.dataset.last_ts)}`
                  : ''
              }${run.dataset.source ? `, from ${run.dataset.source === 'historify' ? 'Historify' : run.dataset.source}` : ''})`
            : '-',
    ],
    [
      'Data window',
      sandbox
        ? String(run.trading_date)
        : scenario.execution_model.trade_from
          ? `${scenario.dataset_spec.interval} bars, trading ${scenario.execution_model.trade_from} to ${scenario.dataset_spec.end}, ${scenario.dataset_spec.exchange}; bars from ${scenario.dataset_spec.start} are history the strategies read`
          : `${scenario.dataset_spec.interval} bars, ${scenario.dataset_spec.start} to ${scenario.dataset_spec.end}, ${scenario.dataset_spec.exchange}`,
    ],
    ['Mode', scenario.mode === 'SHARED' ? 'Shared portfolio' : 'Isolated'],
    ...(run.provenance?.replay
      ? [
          [
            'Isolation',
            run.provenance.replay.os_wall
              ? "The script's own copies of the files it wrote, behind the kernel's wall (Landlock): no network but its simulated OpenAlgo, no writes outside its folder"
              : "The script's own copies of the files it wrote, behind Python-level checks only: this server's kernel has no Landlock",
          ] as [string, string],
        ]
      : []),
    [
      'Costs',
      sandbox
        ? run.environment === 'SHADOW' || hosted
          ? `${scenario.cost_model.preset}, slippage ${scenario.cost_model.slippage_bps} bps, ${hosted ? "on the sandbox's own fill prices" : 'at the latest tick'}`
          : `${scenario.cost_model.preset} on ${run.environment === 'LIVE' ? "the broker's" : "the sandbox's"} own fill prices`
        : run.provenance?.replay
          ? `${scenario.cost_model.preset}, slippage ${scenario.cost_model.slippage_bps} bps, fills as the Sandbox fills them: market orders at the last traded price, resting orders when a later bar reaches them`
          : `${scenario.cost_model.preset}, slippage ${scenario.cost_model.slippage_bps} bps, fills at next bar open`,
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
              <TableHead>Artifact</TableHead>
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
                <TableCell className="font-mono text-xs">
                  {shortHash(inst.strategy_version.model_hash)}
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
