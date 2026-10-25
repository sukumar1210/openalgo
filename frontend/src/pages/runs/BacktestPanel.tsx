// pages/runs/BacktestPanel.tsx
// A Run as it is defined now, backtested over past days, beside the days it
// actually traded in the Sandbox. Each strategy configuration is replayed once,
// then the Run is simulated over the fills by the Sandbox's own rules, so on
// the same fills a backtest's day and a Sandbox day agree.

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { formatPct } from '@/api/research'
import {
  errorMessage,
  getBacktest,
  getPnl,
  listBacktests,
  type Run,
  runKeys,
  startBacktest,
} from '@/api/runs'
import { PortfolioLineChart } from '@/components/portfolio/PortfolioLineChart'
import { Alert, AlertDescription } from '@/components/ui/alert'
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
import { cn } from '@/lib/utils'
import { Kpi, tone, useMoney } from './shared'

const isoOf = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`

function lastMonth(): { from: string; to: string } {
  const now = new Date()
  const first = new Date(now.getFullYear(), now.getMonth() - 1, 1)
  const end = new Date(now.getFullYear(), now.getMonth(), 0)
  return { from: isoOf(first), to: isoOf(end) }
}

/** A replay's research status, in words, for when it has no message of its own. */
const REPLAY_STATE: Record<string, string> = {
  QUEUED: 'Waiting to start',
  RUNNING: 'Replaying',
  COMPLETED: 'Done',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
}

/** The strategies a replay stands for: every member of that configuration, now or upcoming. */
function replayOf(run: Run, fingerprint: string): string {
  const members = [...run.version.members, ...(run.upcoming?.members ?? [])]
  const names = new Set(members.filter((m) => m.fingerprint === fingerprint).map((m) => m.label))
  return names.size ? [...names].join(', ') : 'A strategy'
}

const STATE: Record<string, string> = {
  running: 'Replaying',
  done: 'Done',
  failed: 'Failed',
}

export function BacktestPanel({ run }: { run: Run }) {
  const money = useMoney()
  const client = useQueryClient()
  const [span, setSpan] = useState(lastMonth)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [chosenId, setChosenId] = useState<number | null>(null)
  const list = useQuery({
    queryKey: runKeys.backtests(run.id),
    queryFn: () => listBacktests(run.id),
    refetchInterval: (q) => (q.state.data?.some((b) => b.status === 'running') ? 10_000 : false),
  })
  const rows = list.data ?? []
  const chosen =
    rows.find((b) => b.id === chosenId) ?? rows.find((b) => b.status === 'done') ?? rows[0]
  const detail = useQuery({
    queryKey: runKeys.backtest(run.id, chosen?.id ?? 0),
    queryFn: () => getBacktest(run.id, chosen?.id ?? 0),
    enabled: chosen?.status === 'done',
  })
  const sandbox = useQuery({
    queryKey: runKeys.pnl({ runs: [run.id], from: chosen?.from, to: chosen?.to }),
    queryFn: () => getPnl({ runs: [run.id], from: chosen?.from, to: chosen?.to }),
    enabled: chosen?.status === 'done',
  })

  const start = async () => {
    setError(null)
    setBusy(true)
    try {
      const made = await startBacktest(run.id, span.from, span.to)
      setChosenId(made.id)
      await client.invalidateQueries({ queryKey: runKeys.backtests(run.id) })
    } catch (e) {
      setError(errorMessage(e, 'The backtest could not be started just now. Try again.'))
    } finally {
      setBusy(false)
    }
  }

  const result = detail.data?.result
  const tested = result?.run.daily ?? []
  const traded = sandbox.data?.series[0]?.daily ?? []
  const both = tested.filter((d) => traded.some((t) => t.date === d.date))
  const s = result?.run.summary

  return (
    <Card>
      <CardHeader>
        <CardTitle>A backtest beside its Sandbox days</CardTitle>
        <CardDescription>
          The Run as defined now, replayed over past days: each strategy once, then the Run's
          copies, charges and policy, as the Sandbox applies them.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1">
            <Label htmlFor={`bt-from-${run.id}`}>From</Label>
            <Input
              id={`bt-from-${run.id}`}
              type="date"
              value={span.from}
              onChange={(e) => setSpan((v) => ({ ...v, from: e.target.value }))}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor={`bt-to-${run.id}`}>To</Label>
            <Input
              id={`bt-to-${run.id}`}
              type="date"
              value={span.to}
              onChange={(e) => setSpan((v) => ({ ...v, to: e.target.value }))}
            />
          </div>
          <Button disabled={busy} onClick={start}>
            Backtest this Run
          </Button>
        </div>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {rows.length > 0 && (
          <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Backtests">
            {rows.map((b) => (
              <Button
                key={b.id}
                size="sm"
                role="radio"
                aria-checked={chosen?.id === b.id}
                variant={chosen?.id === b.id ? 'default' : 'outline'}
                onClick={() => setChosenId(b.id)}
              >
                {b.from} to {b.to} · {STATE[b.status] ?? b.status}
              </Button>
            ))}
          </div>
        )}

        {chosen?.status === 'running' && (
          <div className="space-y-1 text-sm">
            {chosen.replays.map((r) => (
              <p key={r.fingerprint} className="text-muted-foreground">
                {replayOf(run, r.fingerprint)}:{' '}
                {r.message ?? REPLAY_STATE[r.status ?? ''] ?? 'Waiting'}
                {r.progress != null && r.status === 'RUNNING'
                  ? ` (${Math.round(r.progress * 100)}%)`
                  : ''}
              </p>
            ))}
          </div>
        )}
        {chosen?.status === 'failed' && (
          <Alert variant="destructive">
            <AlertDescription>{chosen.message}</AlertDescription>
          </Alert>
        )}

        {result && s && (
          <>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Kpi
                label="Net, after charges"
                value={money(s.total_pnl)}
                className={tone(s.total_pnl)}
              />
              <Kpi label="Return on opening capital" value={formatPct(s.return_pct)} />
              <Kpi
                label="Largest drawdown"
                value={`${money(s.max_drawdown)} · ${formatPct(s.max_drawdown_pct)}`}
              />
              <Kpi
                label="Days won"
                value={`${s.winning_days} of ${s.days}`}
                hint={
                  result.refused || result.stopped.length
                    ? `${result.refused} copies refused, ${result.stopped.length} loss-limit days`
                    : undefined
                }
              />
            </div>
            <PortfolioLineChart
              format={money}
              series={[
                {
                  name: 'Backtest',
                  color: '#71717a',
                  data: tested.map((d) => ({ date: d.date, value: d.cumulative })),
                },
                ...(traded.length
                  ? [
                      {
                        name: 'Sandbox',
                        color: '#3b82f6',
                        data: traded.map((d) => ({ date: d.date, value: d.cumulative })),
                      },
                    ]
                  : []),
              ]}
            />
            {both.length > 0 ? (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Day</TableHead>
                      <TableHead className="text-right">Backtest</TableHead>
                      <TableHead className="text-right">Sandbox</TableHead>
                      <TableHead className="text-right">Difference</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {both.map((d) => {
                      const real = traded.find((t) => t.date === d.date)?.net ?? 0
                      return (
                        <TableRow key={d.date}>
                          <TableCell>{d.date}</TableCell>
                          <TableCell className={cn('text-right tabular-nums', tone(d.net))}>
                            {money(d.net)}
                          </TableCell>
                          <TableCell className={cn('text-right tabular-nums', tone(real))}>
                            {money(real)}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {money(real - d.net)}
                          </TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                The Run did not trade in the Sandbox on these days, so there is nothing to set the
                backtest beside yet.
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
