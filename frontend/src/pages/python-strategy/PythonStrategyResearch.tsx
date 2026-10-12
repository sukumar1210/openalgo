// pages/python-strategy/PythonStrategyResearch.tsx
// What Research adds to a Python strategy, on the strategy's own tabs: backtests
// of the unchanged script, the days it traded in the Sandbox, and their results.
// Nothing here has settings of its own: a backtest runs on the script's schedule
// and exchange and the sandbox's capital, and a Sandbox day is recognised by the
// orders the script placed while it was running.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  backtestScript,
  formatPct,
  formatRupees,
  getScriptOverview,
  researchQueryKeys,
  type ScriptOverview,
  type ScriptRunRow,
} from '@/api/research'
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
import { showToast } from '@/utils/toast'
import { StrategyTabs } from './StrategyTabs'

export type ResearchView = 'backtest' | 'sandbox' | 'results'

const STATUS_LABEL: Record<string, string> = {
  QUEUED: 'Waiting',
  RUNNING: 'Running',
  COMPLETED: 'Done',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
}

function isoDay(offsetDays: number): string {
  return new Date(Date.now() + offsetDays * 86_400_000).toLocaleDateString('en-CA', {
    timeZone: 'Asia/Kolkata',
  })
}

function pnl(value: number | null): string {
  return value == null ? '-' : formatRupees(value)
}

function describeSettings(item: ScriptOverview): string {
  const s = item.settings
  const days = s.days.map((d) => d.charAt(0).toUpperCase() + d.slice(1)).join(' ')
  return `${s.entry_time} to ${s.exit_time ?? 'close'}, ${days}, ${s.exchange}, with the sandbox's ${formatRupees(s.capital)} at ${s.leverage}x intraday leverage`
}

export default function PythonStrategyResearch({ view }: { view: ResearchView }) {
  const { strategyId = '' } = useParams<{ strategyId: string }>()
  const overview = useQuery({
    queryKey: researchQueryKeys.script(strategyId),
    queryFn: () => getScriptOverview(strategyId),
    enabled: Boolean(strategyId),
    refetchInterval: (query) =>
      query.state.data?.backtests.some((b) => b.status === 'QUEUED' || b.status === 'RUNNING')
        ? 5000
        : false,
  })

  return (
    <div className="container mx-auto space-y-6 py-6">
      <Button variant="ghost" asChild>
        <Link to="/python">
          <ArrowLeft className="mr-2 h-4 w-4" />
          Back to Python Strategies
        </Link>
      </Button>
      <StrategyTabs strategyId={strategyId} />
      <div>
        <h1 className="text-2xl font-bold tracking-tight">
          {view === 'backtest' ? 'Backtest' : view === 'sandbox' ? 'Sandbox days' : 'Results'}
        </h1>
        <p className="text-muted-foreground">{overview.data?.name ?? strategyId}</p>
      </div>
      {overview.error ? (
        <p className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
          {(overview.error as Error).message}
        </p>
      ) : !overview.data ? (
        <p className="py-8 text-center text-sm text-muted-foreground">Loading...</p>
      ) : view === 'backtest' ? (
        <BacktestView item={overview.data} />
      ) : view === 'sandbox' ? (
        <SandboxView item={overview.data} />
      ) : (
        <ResultsView item={overview.data} />
      )}
    </div>
  )
}

function RunTable({ rows, dated }: { rows: ScriptRunRow[]; dated: boolean }) {
  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{dated ? 'Day' : 'Days'}</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Net P&L</TableHead>
            <TableHead className="text-right">Return</TableHead>
            <TableHead className="text-right">Max drawdown</TableHead>
            <TableHead className="text-right">Trades</TableHead>
            <TableHead />
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={row.id}>
              <TableCell className="whitespace-nowrap">
                {dated ? row.trading_date : `${row.from} to ${row.to}`}
              </TableCell>
              <TableCell>
                <Badge
                  variant={row.status === 'FAILED' ? 'destructive' : 'outline'}
                  title={row.message ?? undefined}
                >
                  {STATUS_LABEL[row.status] ?? row.status}
                </Badge>
                {row.message && row.status !== 'COMPLETED' && (
                  <p className="mt-1 max-w-md text-xs text-muted-foreground">{row.message}</p>
                )}
              </TableCell>
              <TableCell className="text-right">{pnl(row.net_pnl)}</TableCell>
              <TableCell className="text-right">{formatPct(row.return_pct)}</TableCell>
              <TableCell className="text-right">{formatPct(row.max_drawdown_pct)}</TableCell>
              <TableCell className="text-right">{row.trades ?? '-'}</TableCell>
              <TableCell className="text-right">
                <Link to={`/research/runs/${row.id}`} className="text-primary hover:underline">
                  Open
                </Link>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function BacktestView({ item }: { item: ScriptOverview }) {
  const queryClient = useQueryClient()
  const [start, setStart] = useState(isoDay(-31))
  const [end, setEnd] = useState(isoDay(-1))
  const run = useMutation({
    mutationFn: () => backtestScript(item.script_id, start, end),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: researchQueryKeys.script(item.script_id) })
      showToast.success(
        result.created ? 'Backtest started' : 'A backtest of these days is already running'
      )
    },
    onError: (err: Error) => showToast.error(err.message),
  })

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>Backtest this script</CardTitle>
          <CardDescription>
            The script runs unchanged, one trading day at a time on stored market data, exactly as
            Python Strategies runs it: {describeSettings(item)}. It finds the symbols it needs by
            running; bars that are not stored yet are downloaded from your broker first.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="bt-from">From</Label>
              <Input
                id="bt-from"
                type="date"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="bt-to">To</Label>
              <Input id="bt-to" type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
            </div>
            <Button disabled={run.isPending} onClick={() => run.mutate()}>
              Run backtest
            </Button>
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Backtests</CardTitle>
        </CardHeader>
        <CardContent>
          {item.backtests.length ? (
            <RunTable rows={item.backtests} dated={false} />
          ) : (
            <p className="py-6 text-center text-sm text-muted-foreground">No backtests yet.</p>
          )}
        </CardContent>
      </Card>
    </>
  )
}

function SandboxView({ item }: { item: ScriptOverview }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Days it traded in the Sandbox</CardTitle>
        <CardDescription>
          Each day the script trades in analyzer mode is recorded here once it has stopped for the
          day, with the same costs and measures as a backtest.{' '}
          {item.order_tags.length
            ? `Its orders are recognised by the name they carry: ${item.order_tags.join(', ')}.`
            : 'Its orders are recognised by the name they carry, learned the first time it trades.'}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {item.sandbox_days.length ? (
          <RunTable rows={item.sandbox_days} dated />
        ) : (
          <p className="py-6 text-center text-sm text-muted-foreground">
            No Sandbox days yet. They appear after the script's first day of trading in analyzer
            mode.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-lg font-semibold">{value}</p>
    </div>
  )
}

function ResultsView({ item }: { item: ScriptOverview }) {
  const s = item.summary
  const latest = item.backtests.find((b) => b.status === 'COMPLETED')
  return (
    <div className="grid gap-6 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>In the Sandbox</CardTitle>
          <CardDescription>Every recorded day the script traded, after costs.</CardDescription>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-4">
          <Stat label="Net P&L" value={pnl(s.net_pnl)} />
          <Stat label="Days" value={`${s.days} (${s.winning_days} winning)`} />
          <Stat label="Trades" value={String(s.trades)} />
          <Stat label="Best day" value={pnl(s.best_day)} />
          <Stat label="Worst day" value={pnl(s.worst_day)} />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Latest backtest</CardTitle>
          <CardDescription>
            {latest
              ? `${latest.from} to ${latest.to}, the same costs and measures.`
              : 'No finished backtest yet.'}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-4">
          {latest ? (
            <>
              <Stat label="Net P&L" value={pnl(latest.net_pnl)} />
              <Stat label="Return" value={formatPct(latest.return_pct)} />
              <Stat label="Max drawdown" value={formatPct(latest.max_drawdown_pct)} />
              <Stat label="Trades" value={String(latest.trades ?? '-')} />
              <Stat label="Win rate" value={formatPct(latest.win_rate_pct)} />
              <div className="flex items-end">
                <Link to={`/research/runs/${latest.id}`} className="text-primary hover:underline">
                  Open the run
                </Link>
              </div>
            </>
          ) : (
            <Link
              to={`/python/${item.script_id}/backtest`}
              className="text-primary hover:underline"
            >
              Run a backtest
            </Link>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
