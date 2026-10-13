// pages/research/DeploymentDetail.tsx
// A deployed scenario: where its orders go (Sandbox, Shadow or Live), its
// schedule, its strategies, and one session per trading day with its result.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router'
import {
  deploymentAction,
  ENVIRONMENT_LABEL,
  formatPct,
  formatRupees,
  getDeployment,
  researchQueryKeys,
  SANDBOX_ACTIVE,
  type SandboxStatus,
  SESSION_STATUS_LABEL,
  shortHash,
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
import { DeleteDeployment } from './DeleteDeployment'

export function SessionStatusBadge({ status }: { status: SandboxStatus }) {
  const variant =
    status === 'FAILED' ? 'destructive' : status === 'RUNNING' ? 'default' : 'secondary'
  return <Badge variant={variant}>{SESSION_STATUS_LABEL[status]}</Badge>
}

export default function ResearchDeploymentDetail() {
  const { deploymentId } = useParams<{ deploymentId: string }>()
  const id = Number(deploymentId)
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const { data, isLoading, error } = useQuery({
    queryKey: researchQueryKeys.deployment(id),
    queryFn: () => getDeployment(id),
    enabled: Number.isFinite(id),
    refetchInterval: (query) =>
      query.state.data?.sessions.some((s) => SANDBOX_ACTIVE.includes(s.status)) ? 3000 : false,
  })

  const act = useMutation({
    mutationFn: (action: 'stop' | 'activate' | 'start-now') => deploymentAction(id, action),
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

  const active = data.status === 'ACTIVE'

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <Link to="/research" className="text-sm text-muted-foreground hover:underline">
            Research
          </Link>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
            {data.name}
            <Badge variant={data.environment === 'LIVE' ? 'destructive' : 'outline'}>
              {ENVIRONMENT_LABEL[data.environment]}
            </Badge>
            <Badge variant={active ? 'default' : 'secondary'}>
              {active ? 'Active' : 'Stopped'}
            </Badge>
          </h1>
          <p className="text-sm text-muted-foreground">
            Sessions run {data.schedule_start} to {data.schedule_stop} IST on{' '}
            {data.schedule_days.join(', ')}, skipping exchange holidays.{' '}
            {data.environment === 'SANDBOX'
              ? 'Broker orders are off: every order goes to the sandbox.'
              : data.environment === 'SHADOW'
                ? 'Broker orders are off: each order is filled inside the session and sent nowhere.'
                : `Live: real orders go to your broker. It may hold up to ${formatRupees(data.capital_limit ?? 0)}; confirmed ${data.confirmed_at?.slice(0, 16).replace('T', ' ') ?? ''}.`}
          </p>
        </div>
        <div className="flex gap-2">
          {active ? (
            <>
              <Button
                variant="outline"
                onClick={() => act.mutate('start-now')}
                disabled={act.isPending}
              >
                Start today's session now
              </Button>
              <Button variant="outline" onClick={() => act.mutate('stop')} disabled={act.isPending}>
                Stop
              </Button>
            </>
          ) : (
            <>
              <Button onClick={() => act.mutate('activate')} disabled={act.isPending}>
                Activate
              </Button>
              <DeleteDeployment
                id={data.id}
                name={data.name}
                stopped
                onDeleted={() => navigate('/research')}
              />
            </>
          )}
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>So far</CardTitle>
          <CardDescription>Totals over completed sessions.</CardDescription>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Sessions" value={String(data.totals.sessions)} />
          <Stat label="Net P&L" value={formatRupees(data.totals.net_pnl)} />
          <Stat label="Trades" value={String(data.totals.trades)} />
          <Stat label="Costs" value={formatRupees(data.totals.costs)} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Strategies</CardTitle>
          <CardDescription>
            {data.scenario.mode === 'SHARED'
              ? 'One shared portfolio: orders are netted between strategies and each keeps its own P&L.'
              : 'Isolated: each strategy has its own capital, positions and orders.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="space-y-1 text-sm">
            {data.scenario.instances.map((inst) => (
              <li key={inst.position}>
                <span className="font-medium">{inst.strategy_key}</span>{' '}
                <span className="font-mono text-xs text-muted-foreground">
                  {shortHash(inst.strategy_version.version_hash)}
                </span>{' '}
                config v{inst.config_version.version_no},{' '}
                {formatRupees(inst.config_version.config.capital)},{' '}
                {inst.config_version.config.leverage}x, {inst.config_version.config.entry_time}-
                {inst.config_version.config.exit_time}
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Sessions</CardTitle>
          <CardDescription>One per trading day.</CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          {data.sessions.length === 0 ? (
            <p className="py-4 text-sm text-muted-foreground">
              No sessions yet. The first one opens at the next scheduled start.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Day</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Net P&amp;L</TableHead>
                  <TableHead className="text-right">Return</TableHead>
                  <TableHead className="text-right">Trades</TableHead>
                  <TableHead>Note</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.sessions.map((s) => (
                  <TableRow key={s.id}>
                    <TableCell>
                      <Link to={`/research/sessions/${s.id}`} className="hover:underline">
                        {s.trading_date}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <SessionStatusBadge status={s.status} />
                    </TableCell>
                    <TableCell className="text-right">
                      {s.metrics ? formatRupees(s.metrics.net_pnl) : '-'}
                    </TableCell>
                    <TableCell className="text-right">
                      {s.metrics ? formatPct(s.metrics.return_pct) : '-'}
                    </TableCell>
                    <TableCell className="text-right">{s.metrics?.trades ?? '-'}</TableCell>
                    <TableCell className="max-w-md text-sm">{s.message ?? ''}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
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
