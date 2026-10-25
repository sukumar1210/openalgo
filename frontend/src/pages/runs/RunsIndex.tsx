// pages/runs/RunsIndex.tsx
// Every Run at a glance: how each is set up, its capital and its day so far,
// and the sources trading for them. A Run trades in Sandbox accounts of its
// own; nothing here reaches the broker.

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { getSources, listRuns, MODE_LABEL, POLICY_LABEL, runKeys } from '@/api/runs'
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
import {
  firstDay,
  Kpi,
  netSinceStart,
  RunsNav,
  runToday,
  StatusBadge,
  tone,
  useMoney,
} from './shared'

export default function RunsIndex() {
  const money = useMoney()
  const navigate = useNavigate()
  const [chosen, setChosen] = useState<number[]>([])
  const runs = useQuery({ queryKey: runKeys.list(), queryFn: listRuns, refetchInterval: 30_000 })
  const sources = useQuery({ queryKey: runKeys.sources(), queryFn: getSources })

  const rows = runs.data ?? []
  const active = rows.filter((r) => r.status === 'active')
  const today = active.map(runToday)
  const netToday = today.reduce((t, d) => t + (d.net ?? 0), 0)
  const capitalToday = today.reduce((t, d) => t + d.capital, 0)
  const running = (sources.data ?? []).filter((s) => s.status === 'active')

  const toggle = (id: number) =>
    setChosen((ids) => (ids.includes(id) ? ids.filter((i) => i !== id) : [...ids, id]))

  return (
    <div className="container mx-auto space-y-6 px-4 py-6">
      <RunsNav />
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Runs</h1>
          <p className="text-muted-foreground">
            Each Run trades some of your Python strategies together, in Sandbox accounts of its own.
            Nothing here reaches your broker.
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            disabled={chosen.length < 2}
            onClick={() => navigate(`/runs/compare?runs=${chosen.join(',')}`)}
          >
            Compare selected
          </Button>
          <Button asChild>
            <Link to="/runs/new">New Run</Link>
          </Button>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi label="Active Runs" value={active.length} />
        <Kpi
          label="Net today, all Runs, after charges"
          value={money(netToday)}
          className={tone(netToday)}
        />
        <Kpi label="Capital in Runs today" value={money(capitalToday)} />
        <Kpi
          label="Strategies running for Runs"
          value={running.length}
          hint="One process per configuration, however many Runs copy it."
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Runs</CardTitle>
          <CardDescription>Tick two or more to compare them on one chart.</CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          {runs.error ? (
            <p className="text-sm text-destructive">
              Your Runs could not be read just now. Refresh the page to try again.
            </p>
          ) : !runs.isLoading && rows.length === 0 ? (
            <div className="py-10 text-center">
              <p className="text-muted-foreground">
                No Runs yet. A Run trades some of your Python strategies together with money of its
                own, in the Sandbox.
              </p>
              <Button asChild className="mt-4">
                <Link to="/runs/new">New Run</Link>
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8">
                    <span className="sr-only">Compare</span>
                  </TableHead>
                  <TableHead>Run</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Mode and policy</TableHead>
                  <TableHead>Strategies</TableHead>
                  <TableHead className="text-right">Capital today</TableHead>
                  <TableHead className="text-right">Net today</TableHead>
                  <TableHead className="text-right">Net since start</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((run) => {
                  const day = runToday(run)
                  const since = netSinceStart(run)
                  const start = firstDay(run)
                  return (
                    <TableRow key={run.id}>
                      <TableCell>
                        <input
                          type="checkbox"
                          aria-label={`Compare ${run.name}`}
                          checked={chosen.includes(run.id)}
                          onChange={() => toggle(run.id)}
                          className="h-4 w-4"
                        />
                      </TableCell>
                      <TableCell>
                        <Link to={`/runs/${run.id}`} className="font-medium hover:underline">
                          {run.name}
                        </Link>
                        <div className="text-xs text-muted-foreground">
                          {money(run.version.capital)}
                          {start
                            ? ` · since ${start}`
                            : run.status === 'draft'
                              ? ' · not started yet'
                              : ''}
                        </div>
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={run.status} />
                        {day.stopped.length > 0 && (
                          <div className="mt-1 text-xs text-red-600 dark:text-red-500">
                            Loss limit reached today
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="whitespace-nowrap">
                        {MODE_LABEL[run.version.mode]} · {POLICY_LABEL[run.version.policy]}
                      </TableCell>
                      <TableCell>{run.version.members.map((m) => m.label).join(', ')}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {run.status === 'draft' ? '-' : money(day.capital)}
                      </TableCell>
                      <TableCell className={cn('text-right tabular-nums', tone(day.net))}>
                        {day.net == null ? '-' : money(day.net)}
                      </TableCell>
                      <TableCell className={cn('text-right tabular-nums', tone(since))}>
                        {since == null ? '-' : money(since)}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
          {runs.isLoading && <p className="py-6 text-sm text-muted-foreground">Loading...</p>}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Strategies running for Runs</CardTitle>
            <CardDescription>
              One process per configuration. Runs that hold the same strategy with the same settings
              copy the same one, each scaled to its own capital.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {(sources.data ?? []).length === 0 ? (
              <p className="text-sm text-muted-foreground">
                None yet. Starting a Run starts what it copies.
              </p>
            ) : (
              (sources.data ?? []).map((s) => (
                <div
                  key={s.fingerprint}
                  className="flex flex-wrap items-center justify-between gap-2 border-b py-2 text-sm last:border-0"
                >
                  <span className="font-medium">{s.name}</span>
                  <span className="text-muted-foreground">
                    {s.status !== 'active'
                      ? 'Retired'
                      : s.running
                        ? 'Running'
                        : s.schedule
                          ? `Starts at ${s.schedule.start}`
                          : 'Waiting'}
                    {s.runs.length > 0 &&
                      ` · copied by ${s.runs.length} Run${s.runs.length > 1 ? 's' : ''}`}
                  </span>
                </div>
              ))
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Outside Runs</CardTitle>
            <CardDescription>
              Strategies started from Strategies keep trading the main Sandbox account, as before.
              Their P&L sits beside the Runs on the P&L page.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button asChild variant="outline">
              <Link to="/runs/pnl">Open P&L</Link>
            </Button>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
