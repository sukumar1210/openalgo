// pages/research/SessionDetail.tsx
// One Sandbox session, live: market data, each strategy's book and positions,
// open orders, the last signal and fill, and what happened in order. A
// completed session also opens in the run page with its full results.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'
import {
  controlSession,
  ENVIRONMENT_LABEL,
  formatIstTime,
  formatRupees,
  getSandboxSession,
  type LiveBook,
  researchQueryKeys,
  SANDBOX_ACTIVE,
  type SessionEnvironment,
  type SessionEvent,
} from '@/api/research'
import { Badge } from '@/components/ui/badge'
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
import { showToast } from '@/utils/toast'
import { SessionStatusBadge } from './DeploymentDetail'

const MARKET_LABEL = {
  CONNECTING: 'Connecting',
  WAITING: 'Waiting for the first tick',
  LIVE: 'Live',
  STALE: 'No ticks recently: new trades paused',
} as const

const EVENT_LABEL: Record<string, string> = {
  ORDER_INTENT: 'Order decided',
  ORDER_SUBMITTED: 'Sent to the sandbox',
  ORDER_FILLED: 'Filled',
  ORDER_REJECTED: 'Refused by the sandbox',
  ORDER_UNFILLED: 'Not filled',
  SESSION_EVENT: 'Session',
}

function describeEvent(event: SessionEvent): string {
  const p = event.payload as Record<string, unknown>
  switch (event.kind) {
    case 'ORDER_INTENT':
      return `${p.symbol} ${Number(p.net) > 0 ? 'buy' : 'sell'} ${Math.abs(Number(p.net))} at about ${Number(p.reference_price).toFixed(2)}`
    case 'ORDER_FILLED':
      return `at ${Number(p.price).toFixed(2)}`
    case 'ORDER_REJECTED':
    case 'ORDER_UNFILLED':
      return String(p.reason ?? '')
    case 'SESSION_EVENT':
      return String(p.event ?? '').replaceAll('_', ' ')
    default:
      return ''
  }
}

const ENVIRONMENT_SENTENCE: Record<SessionEnvironment, string> = {
  SANDBOX: "Broker orders off. Every order is filled by OpenAlgo's sandbox.",
  SHADOW:
    'Broker orders off. Nothing is sent anywhere: each order is filled at the latest price inside the session.',
  LIVE: "Live: real orders go to your broker, within this deployment's limit.",
}

export default function ResearchSessionDetail() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const id = Number(sessionId)
  const queryClient = useQueryClient()

  const { data, isLoading, error } = useQuery({
    queryKey: researchQueryKeys.session(id),
    queryFn: () => getSandboxSession(id),
    enabled: Number.isFinite(id),
    refetchInterval: (query) =>
      query.state.data && SANDBOX_ACTIVE.includes(query.state.data.status) ? 2000 : false,
  })

  const control = useMutation({
    mutationFn: (action: 'pause' | 'resume' | 'stop') => controlSession(id, action),
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

  const live = data.live_state
  const books = live?.books ?? []
  const shared = books.length > 1 && books.every((b) => b.portfolio === books[0].portfolio)
  const active = SANDBOX_ACTIVE.includes(data.status)

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <Link
            to={`/research/deployments/${data.deployment_id}`}
            className="text-sm text-muted-foreground hover:underline"
          >
            Deployment
          </Link>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
            Session #{data.id}, {data.trading_date}
            <Badge variant={data.environment === 'LIVE' ? 'destructive' : 'outline'}>
              {ENVIRONMENT_LABEL[data.environment]}
            </Badge>
            <SessionStatusBadge status={data.status} />
          </h1>
          <p
            className={`text-sm ${data.environment === 'LIVE' ? 'font-medium text-destructive' : 'text-muted-foreground'}`}
          >
            {ENVIRONMENT_SENTENCE[data.environment]}
            {data.message ? ` ${data.message}` : ''}
          </p>
          {data.same_day.length > 0 && (
            <p className="text-sm">
              Same scenario today:{' '}
              {data.same_day.map((other, n) => (
                <span key={other.id}>
                  {n > 0 && ', '}
                  <Link
                    to={`/research/compare/${data.id}/${other.id}`}
                    className="underline-offset-2 hover:underline"
                  >
                    compare with the {ENVIRONMENT_LABEL[other.environment]} session
                  </Link>
                </span>
              ))}
            </p>
          )}
        </div>
        <div className="flex gap-2">
          {data.status === 'RUNNING' && (
            <Button variant="outline" onClick={() => control.mutate('pause')}>
              Pause new trades
            </Button>
          )}
          {data.status === 'PAUSED' && (
            <Button variant="outline" onClick={() => control.mutate('resume')}>
              Resume
            </Button>
          )}
          {active && data.status !== 'STOPPING' && (
            <Button variant="outline" onClick={() => control.mutate('stop')}>
              Close positions and stop
            </Button>
          )}
          <Button asChild variant={data.status === 'COMPLETED' ? 'default' : 'outline'}>
            <Link to={`/research/runs/${data.id}`}>
              {data.status === 'COMPLETED' ? 'Full results' : 'Ledger'}
            </Link>
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Now</CardTitle>
          <CardDescription>
            Market data: {live ? MARKET_LABEL[live.market_data.status] : 'Not started'}
            {live?.market_data.last_tick_at
              ? `, last tick ${formatIstTime(live.market_data.last_tick_at)}`
              : ''}
            . New trades: {live?.accept_new ? 'on' : 'off'}.
            {live?.venue?.answering === false && live.venue.since
              ? ` ${data.environment === 'LIVE' ? 'The broker' : 'The sandbox'} has not answered since ${formatIstTime(live.venue.since)}.`
              : live?.venue?.answering && data.environment !== 'SHADOW'
                ? ` ${data.environment === 'LIVE' ? 'Broker' : 'Sandbox'}: answering.`
                : ''}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Stat
            label={shared ? 'Portfolio equity' : 'Equity, all strategies'}
            value={formatRupees(books.reduce((sum, b) => sum + b.equity, 0))}
          />
          <Stat
            label="Last signal"
            value={
              live?.last_signal
                ? `${books.find((b) => b.instance === live.last_signal?.instance)?.label ?? 'Strategy'}: ${
                    Object.entries(live.last_signal.weights)
                      .map(
                        ([symbol, w]) =>
                          `${symbol} ${w === null ? '?' : `${(w * 100).toFixed(0)}%`}`
                      )
                      .join(', ') || 'flat'
                  }`
                : '-'
            }
          />
          <Stat
            label="Last order"
            value={
              live?.last_order
                ? `${live.last_order.symbol} ${live.last_order.net > 0 ? 'buy' : 'sell'} ${Math.abs(live.last_order.net)}`
                : '-'
            }
          />
          <Stat
            label="Last fill"
            value={
              live?.last_fill
                ? `${live.last_fill.symbol} ${live.last_fill.qty > 0 ? '+' : ''}${live.last_fill.qty} @ ${live.last_fill.price.toFixed(2)}`
                : '-'
            }
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>By strategy</CardTitle>
          <CardDescription>
            Each strategy's own book. The sandbox account is shared and may merge positions; these
            books do not.
            {shared && ` Orders between strategies that cancelled out: ${live?.conflicts ?? 0}.`}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {books.length === 0 ? (
            <p className="text-sm text-muted-foreground">The session has not started trading.</p>
          ) : (
            books.map((book) => <BookCard key={book.instance} book={book} />)
          )}
        </CardContent>
      </Card>

      {(live?.open_orders ?? []).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Orders waiting for the sandbox</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1 text-sm">
              {(live?.open_orders ?? []).map((o) => (
                <li key={o.platform_order_id}>
                  {o.symbol} {o.net > 0 ? 'buy' : 'sell'} {Math.abs(o.net)}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>What happened</CardTitle>
          <CardDescription>The latest 100 events, newest first.</CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Event</TableHead>
                <TableHead>Detail</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.events.map((e) => (
                <TableRow key={e.seq}>
                  <TableCell className="whitespace-nowrap">
                    {formatIstTime(Date.parse(e.at) / 1000)}
                  </TableCell>
                  <TableCell>{EVENT_LABEL[e.kind] ?? e.kind}</TableCell>
                  <TableCell className="text-sm">{describeEvent(e)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}

function BookCard({ book }: { book: LiveBook }) {
  const positions = Object.entries(book.positions)
  const pnl = book.equity - book.capital
  return (
    <div className="rounded-md border p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium">{book.label}</span>
        <span className="text-sm">
          Capital {formatRupees(book.capital)}, equity {formatRupees(book.equity)}, P&amp;L{' '}
          {formatRupees(pnl)}
        </span>
      </div>
      {positions.length === 0 ? (
        <p className="pt-2 text-sm text-muted-foreground">Flat.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Symbol</TableHead>
              <TableHead className="text-right">Qty</TableHead>
              <TableHead className="text-right">Average</TableHead>
              <TableHead className="text-right">Last</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {positions.map(([symbol, p]) => (
              <TableRow key={symbol}>
                <TableCell>{symbol}</TableCell>
                <TableCell className="text-right">{p.qty}</TableCell>
                <TableCell className="text-right">{p.avg?.toFixed(2) ?? '-'}</TableCell>
                <TableCell className="text-right">{p.mark?.toFixed(2) ?? '-'}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
    </div>
  )
}
