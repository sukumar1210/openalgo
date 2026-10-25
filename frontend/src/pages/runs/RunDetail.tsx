// pages/runs/RunDetail.tsx
// One Run: how it stands today (capital, the day so far, what each account
// holds, every copy and its result, the sources it copies), its days, its
// backtests, and its definition and history.

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import {
  actOnRun,
  deleteRun,
  errorMessage,
  getRun,
  MODE_LABEL,
  POLICY_LABEL,
  type Run,
  type RunAction,
  type RunSource,
  type RunVersion,
  runKeys,
} from '@/api/runs'
import { Alert, AlertDescription } from '@/components/ui/alert'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'
import { BacktestPanel } from './BacktestPanel'
import { PnlReport } from './PnlReport'
import {
  describeEvent,
  istTime,
  Kpi,
  RunsNav,
  runToday,
  StatusBadge,
  tone,
  useMoney,
} from './shared'

const LIMIT_LABEL = { capital: "each strategy's capital that day", fixed: 'each allocation' }

export default function RunDetail() {
  const { runId } = useParams()
  const id = Number(runId)
  const run = useQuery({
    queryKey: runKeys.run(id),
    queryFn: () => getRun(id),
    refetchInterval: 15_000,
  })

  if (run.error) {
    return (
      <div className="container mx-auto space-y-4 px-4 py-6">
        <RunsNav />
        <p className="text-sm text-destructive">
          {errorMessage(run.error, 'This Run could not be read just now. Refresh the page.')}
        </p>
      </div>
    )
  }
  if (!run.data) {
    return (
      <div className="container mx-auto space-y-4 px-4 py-6">
        <RunsNav />
        <p className="text-sm text-muted-foreground">Loading...</p>
      </div>
    )
  }
  return <RunPage run={run.data} />
}

function RunPage({ run }: { run: Run }) {
  const v = run.version
  return (
    <div className="container mx-auto space-y-6 px-4 py-6">
      <RunsNav />
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link to="/runs" className="text-sm hover:underline">
            Runs
          </Link>
          <h1 className="flex flex-wrap items-center gap-3 text-2xl font-bold tracking-tight">
            {run.name} <StatusBadge status={run.status} />
          </h1>
          <p className="text-muted-foreground">
            {MODE_LABEL[v.mode]} · {POLICY_LABEL[v.policy]} · Limit: {LIMIT_LABEL[v.limit]}
            {v.loss_limit_pct ? ` · Daily loss limit ${v.loss_limit_pct}%` : ''} · Version{' '}
            {v.number} since {v.effective_from}
          </p>
        </div>
        <Actions run={run} />
      </div>

      {run.upcoming && (
        <Alert>
          <AlertDescription>
            An edit applies from {run.upcoming.effective_from} (version {run.upcoming.number}).
            Today finishes on what it started with.
          </AlertDescription>
        </Alert>
      )}

      <Tabs defaultValue="today">
        <TabsList>
          <TabsTrigger value="today">Today</TabsTrigger>
          <TabsTrigger value="days">Days and P&L</TabsTrigger>
          <TabsTrigger value="backtests">Backtests</TabsTrigger>
          <TabsTrigger value="settings">Settings and history</TabsTrigger>
        </TabsList>
        <TabsContent value="today" className="space-y-6 pt-4">
          <Today run={run} />
        </TabsContent>
        <TabsContent value="days" className="pt-4">
          {run.days.length ? (
            <PnlReport query={{ runs: [run.id] }} view="individual" />
          ) : (
            <p className="py-10 text-center text-sm text-muted-foreground">
              No days yet: a Run's first day opens at 09:05 on the trading day after it starts, or
              at once inside the session.
            </p>
          )}
        </TabsContent>
        <TabsContent value="backtests" className="pt-4">
          <BacktestPanel run={run} />
        </TabsContent>
        <TabsContent value="settings" className="space-y-6 pt-4">
          <Definition title="As it trades now" version={v} />
          {run.upcoming && (
            <Definition title={`From ${run.upcoming.effective_from}`} version={run.upcoming} />
          )}
          <History run={run} />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function Actions({ run }: { run: Run }) {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const act = async (action: RunAction) => {
    setError(null)
    setBusy(true)
    try {
      await actOnRun(run.id, action)
      await client.invalidateQueries({ queryKey: runKeys.all })
    } catch (e) {
      setError(errorMessage(e, 'The Run could not be changed just now. Try again.'))
    } finally {
      setBusy(false)
    }
  }
  const remove = async () => {
    setError(null)
    try {
      await deleteRun(run.id)
      await client.invalidateQueries({ queryKey: runKeys.all })
      navigate('/runs')
    } catch (e) {
      setError(errorMessage(e, 'The Run could not be deleted just now. Try again.'))
    }
  }
  const live = run.status === 'active' || run.status === 'paused'

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap justify-end gap-2">
        {run.status !== 'stopped' && (
          <Button variant="outline" asChild>
            <Link to={`/runs/${run.id}/edit`}>Edit</Link>
          </Button>
        )}
        {(run.status === 'draft' || run.status === 'paused') && (
          <Button disabled={busy} onClick={() => act('start')}>
            {run.status === 'paused' ? 'Resume' : 'Start'}
          </Button>
        )}
        {run.status === 'active' && (
          <Button variant="outline" disabled={busy} onClick={() => act('pause')}>
            Pause
          </Button>
        )}
        {live && (
          <Confirm
            label="Stop"
            title={`Stop ${run.name} for good?`}
            text="It copies nothing more and cannot be started again. Its accounts keep their history; whatever they hold is closed at today's square-off."
            onConfirm={() => act('stop')}
          />
        )}
        {live && (
          <Confirm
            label="Kill switch"
            destructive
            title="Close everything this Run holds, now?"
            text="It is paused and every position its accounts hold is closed at once, at the market. Start it again to resume copying."
            onConfirm={() => act('kill')}
          />
        )}
        {run.status === 'draft' && (
          <Confirm
            label="Delete"
            title={`Delete ${run.name}?`}
            text="It never traded, so nothing is lost but its definition and its backtests."
            onConfirm={remove}
          />
        )}
      </div>
      {error && <p className="text-right text-sm text-destructive">{error}</p>}
    </div>
  )
}

function Confirm({
  label,
  title,
  text,
  onConfirm,
  destructive,
}: {
  label: string
  title: string
  text: string
  onConfirm: () => void
  destructive?: boolean
}) {
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant={destructive ? 'destructive' : 'outline'}>{label}</Button>
      </AlertDialogTrigger>
      <AlertDialogContent role="alertdialog">
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{text}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm}>{label}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function sourceFor(sources: RunSource[] | undefined, fingerprint: string) {
  return sources?.find((s) => s.fingerprint === fingerprint)
}

function Today({ run }: { run: Run }) {
  const money = useMoney()
  const day = runToday(run)
  const copies = run.copies ?? []
  const shared = run.version.mode === 'shared'
  const seatOf = (seat: string) => run.today.seats.find((s) => s.seat === (shared ? 'run' : seat))
  const counted = (seat: string, status: string) =>
    copies.filter((c) => c.seat === seat && c.status === status).length
  const todaysEvents = (run.events ?? []).filter((e) => e.at?.slice(0, 10) === run.today.date)

  return (
    <>
      {!day.opened && (
        <p className="text-sm text-muted-foreground">
          {run.status === 'draft'
            ? 'A draft trades nothing until it is started.'
            : run.status === 'active'
              ? 'Today has not opened yet: a Run opens at 09:05 on a trading day.'
              : 'Not trading today.'}
        </p>
      )}
      {day.stopped.length > 0 && (
        <Alert variant="destructive">
          <AlertDescription>
            {day.stopped.join(', ')} reached the daily loss limit: its intraday positions were
            squared off and it opens nothing more today.
          </AlertDescription>
        </Alert>
      )}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi
          label="Capital today"
          value={money(day.capital)}
          hint={
            day.opened
              ? `Moved at the open: ${money(run.today.seats.reduce((t, s) => t + (s.transfer ?? 0), 0))}`
              : undefined
          }
        />
        <Kpi
          label="Net today, after charges"
          value={day.net == null ? '-' : money(day.net)}
          className={tone(day.net)}
        />
        <Kpi label="Charges today" value={day.charges == null ? '-' : money(day.charges)} />
        <Kpi
          label="Holding now, of its limit"
          value={`${money(day.exposure)}${day.limit ? ` / ${money(day.limit)}` : ''}`}
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Strategies</CardTitle>
          <CardDescription>
            {shared
              ? 'One account holds them all; each copies its source into it under its own order name.'
              : 'Each trades an account of its own, copying its source scaled to its capital.'}
          </CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Strategy</TableHead>
                <TableHead className="text-right">Capital today</TableHead>
                <TableHead className="text-right">Limit</TableHead>
                <TableHead className="text-right">Holding now</TableHead>
                <TableHead className="text-right">Net today</TableHead>
                <TableHead>Copies today</TableHead>
                <TableHead>Copies</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {run.version.members.map((m) => {
                const seat = seatOf(m.seat)
                const capital =
                  run.today.members.find((x) => x.seat === m.seat)?.capital ?? m.allocated
                const source = sourceFor(run.sources, m.fingerprint)
                const filled = counted(m.seat, 'placed')
                const refused = counted(m.seat, 'refused')
                return (
                  <TableRow key={m.seat}>
                    <TableCell className="font-medium">{m.label}</TableCell>
                    <TableCell className="text-right tabular-nums">{money(capital)}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {shared ? '-' : seat?.limit == null ? '-' : money(seat.limit)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {shared ? '-' : money(seat?.exposure ?? 0)}
                    </TableCell>
                    <TableCell className={cn('text-right tabular-nums', tone(seat?.net))}>
                      {shared || seat?.net == null ? '-' : money(seat.net)}
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                      {filled} filled{refused ? ` · ${refused} refused` : ''}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {source
                        ? m.sizing === 'self'
                          ? `${source.name}, its own quantity`
                          : `${source.name} at ${((capital / source.reference_capital) * 100).toFixed(2)}%`
                        : 'Starts with the Run'}
                    </TableCell>
                  </TableRow>
                )
              })}
              {shared && seatOf('run') && (
                <TableRow className="bg-muted/40">
                  <TableCell className="font-medium">The shared account</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {money(seatOf('run')?.opening ?? 0)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {seatOf('run')?.limit == null ? '-' : money(seatOf('run')?.limit ?? 0)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {money(seatOf('run')?.exposure ?? 0)}
                  </TableCell>
                  <TableCell className={cn('text-right tabular-nums', tone(seatOf('run')?.net))}>
                    {seatOf('run')?.net == null ? '-' : money(seatOf('run')?.net ?? 0)}
                  </TableCell>
                  <TableCell colSpan={2} />
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {run.today.seats.some((s) => s.positions.length) && (
        <Card>
          <CardHeader>
            <CardTitle>Holding now</CardTitle>
            <CardDescription>
              Intraday positions; every account squares off its own at 15:15.
            </CardDescription>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Account</TableHead>
                  <TableHead>Symbol</TableHead>
                  <TableHead className="text-right">Quantity</TableHead>
                  <TableHead className="text-right">Average</TableHead>
                  <TableHead className="text-right">Last</TableHead>
                  <TableHead className="text-right">P&L</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {run.today.seats.flatMap((s) =>
                  s.positions.map((p) => (
                    <TableRow key={`${s.seat}-${p.symbol}-${p.exchange}`}>
                      <TableCell>{s.label}</TableCell>
                      <TableCell>
                        {p.symbol}{' '}
                        <span className="text-xs text-muted-foreground">{p.exchange}</span>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{p.quantity}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {money(p.average_price)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{money(p.ltp)}</TableCell>
                      <TableCell className={cn('text-right tabular-nums', tone(p.pnl))}>
                        {money(p.pnl)}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Copies today</CardTitle>
          <CardDescription>
            Each source trade, copied into each account that follows it at the source's price.
          </CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          {copies.length === 0 ? (
            <p className="text-sm text-muted-foreground">Nothing copied yet today.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Time</TableHead>
                  <TableHead>Strategy</TableHead>
                  <TableHead>Symbol</TableHead>
                  <TableHead>Side</TableHead>
                  <TableHead className="text-right">Quantity</TableHead>
                  <TableHead className="text-right">Price</TableHead>
                  <TableHead>Result</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {copies.map((c, i) => (
                  <TableRow key={`${c.time}-${c.member}-${i}`}>
                    <TableCell className="tabular-nums">{c.time ?? '-'}</TableCell>
                    <TableCell>{c.member}</TableCell>
                    <TableCell>{c.symbol ?? '-'}</TableCell>
                    <TableCell>{c.action === 'BUY' ? 'Buy' : 'Sell'}</TableCell>
                    <TableCell className="text-right tabular-nums">{c.quantity}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {c.price == null ? '-' : money(c.price)}
                    </TableCell>
                    <TableCell
                      className={cn(c.status === 'refused' && 'text-red-600 dark:text-red-500')}
                    >
                      {c.status === 'placed'
                        ? "Filled at the source's price"
                        : c.status === 'refused'
                          ? `Refused: ${c.reason ?? 'not placed'}. The next trade catches up.`
                          : 'Being placed'}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>What it copies</CardTitle>
            <CardDescription>
              One process per configuration, trading Rs 1 crore or more so members scale down from
              it.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            {(run.sources ?? []).length === 0 ? (
              <p className="text-muted-foreground">Its sources start when the Run starts.</p>
            ) : (
              (run.sources ?? []).map((s) => (
                <div key={s.fingerprint} className="border-b py-2 last:border-0">
                  <div className="font-medium">{s.name}</div>
                  <div className="text-muted-foreground">
                    {s.running
                      ? 'Running'
                      : s.status === 'active'
                        ? 'Waiting for its schedule'
                        : 'Retired'}{' '}
                    · trades {money(s.reference_capital)}
                    {s.runs.length > 1 ? ` · shared with ${s.runs.length - 1} other Run(s)` : ''}
                  </div>
                </div>
              ))
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Today</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-sm">
            {todaysEvents.length === 0 ? (
              <p className="text-muted-foreground">Nothing yet today.</p>
            ) : (
              todaysEvents.map((e, i) => (
                <div key={`${e.at}-${i}`} className="flex gap-3">
                  <span className="w-28 shrink-0 tabular-nums text-muted-foreground">
                    {istTime(e.at)}
                  </span>
                  <span>{describeEvent(e)}</span>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>
    </>
  )
}

function Definition({ title, version }: { title: string; version: RunVersion }) {
  const money = useMoney()
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>
          Version {version.number}: {money(version.capital)} · {MODE_LABEL[version.mode]} ·{' '}
          {POLICY_LABEL[version.policy]} · limit {LIMIT_LABEL[version.limit]}
          {version.loss_limit_pct ? ` · daily loss limit ${version.loss_limit_pct}%` : ''}
        </CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Strategy</TableHead>
              <TableHead className="text-right">Allocation</TableHead>
              <TableHead>Sized by</TableHead>
              <TableHead>Script version</TableHead>
              <TableHead>Schedule</TableHead>
              <TableHead>Order name</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {version.members.map((m) => (
              <TableRow key={m.seat}>
                <TableCell className="font-medium">{m.label}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {money(m.allocated)}
                  {m.allocation_kind === 'percent' && (
                    <span className="text-xs text-muted-foreground"> ({m.allocation_value}%)</span>
                  )}
                </TableCell>
                <TableCell>{m.sizing === 'self' ? 'Its own quantity' : 'Its capital'}</TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {m.settings.file_name} · {m.settings.code_hash.slice(0, 10)}
                </TableCell>
                <TableCell className="whitespace-nowrap text-xs">
                  {m.settings.exchange} · {m.settings.schedule.start} to{' '}
                  {m.settings.schedule.stop ?? 'the close'}
                  {m.settings.symbols.length ? ` · ${m.settings.symbols.length} symbols` : ''}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">{m.order_name}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

function History({ run }: { run: Run }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>What happened</CardTitle>
        <CardDescription>Newest first.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-1 text-sm">
        {(run.events ?? []).map((e, i) => (
          <div key={`${e.at}-${i}`} className="flex gap-3">
            <span className="w-28 shrink-0 tabular-nums text-muted-foreground">
              {istTime(e.at)}
            </span>
            <span>{describeEvent(e)}</span>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}
