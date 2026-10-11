// pages/research/List.tsx
// Research home: the strategies this server can backtest, and their current versions.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router'
import {
  createExperiment,
  ENVIRONMENT_LABEL,
  EXPERIMENT_STATUS_LABEL,
  formatRupees,
  listDeployments,
  listExperiments,
  listStrategies,
  researchQueryKeys,
  SESSION_STATUS_LABEL,
  STRATEGY_STATUS_LABEL,
  shortHash,
} from '@/api/research'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { showToast } from '@/utils/toast'
import { LivePanel } from './LivePanel'
import { OperationsPanel } from './OperationsPanel'
import { StrategyActions } from './StrategyActions'

export default function ResearchList() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [objective, setObjective] = useState('')
  const { data, isLoading, error } = useQuery({
    queryKey: researchQueryKeys.strategies(),
    queryFn: listStrategies,
  })
  const experiments = useQuery({
    queryKey: researchQueryKeys.experiments(),
    queryFn: listExperiments,
  })
  const deployments = useQuery({
    queryKey: researchQueryKeys.deployments(),
    queryFn: listDeployments,
    refetchInterval: 10_000,
  })
  const create = useMutation({
    mutationFn: () => createExperiment(name, '', objective),
    onSuccess: (item) => {
      queryClient.invalidateQueries({ queryKey: researchQueryKeys.experiments() })
      navigate(`/research/experiments/${item.id}`)
    },
    onError: (err: Error) => showToast.error(err.message),
  })

  const rows = data?.items ?? []
  const problems = data?.problems ?? []

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Research</h1>
        <p className="text-sm text-muted-foreground">
          Backtest strategies against stored market data, then run them on live market data in the
          Sandbox, in Shadow or live. Every strategy version and every configuration is kept exactly
          as it was, so a past result always shows what produced it. Only a live deployment sends
          orders to your broker.
        </p>
      </div>

      {problems.length > 0 && (
        <div className="space-y-1 rounded-md bg-destructive/10 p-3 text-sm text-destructive">
          {problems.map((p) => (
            <p key={p.key}>
              <span className="font-medium">{p.key}:</span> {p.message}
            </p>
          ))}
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Strategies</CardTitle>
          <CardDescription>
            Strategies come from installed strategy packages. A new package version appears here as
            a new strategy version; older versions stay available.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {error ? (
            <p className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
              {(error as Error).message}
            </p>
          ) : isLoading ? (
            <p className="py-8 text-center text-sm text-muted-foreground">Loading…</p>
          ) : rows.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              No strategies are installed yet.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Strategy</TableHead>
                    <TableHead>Version</TableHead>
                    <TableHead>Bars</TableHead>
                    <TableHead>Current config</TableHead>
                    <TableHead>Source</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Last result</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((row) => (
                    <TableRow key={row.id}>
                      <TableCell className="font-medium">
                        <Link to={`/research/strategies/${row.id}`} className="hover:underline">
                          {row.key}
                        </Link>
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {row.latest_version
                          ? `${row.latest_version.dist_version} / ${shortHash(row.latest_version.version_hash)}`
                          : '-'}
                      </TableCell>
                      <TableCell>{row.latest_version?.interval ?? '-'}</TableCell>
                      <TableCell>
                        {row.latest_config ? (
                          <span>
                            v{row.latest_config.version_no} -{' '}
                            {formatRupees(row.latest_config.config.capital)},{' '}
                            {row.latest_config.config.leverage}x,{' '}
                            {row.latest_config.config.universe.length} symbols
                          </span>
                        ) : (
                          <span className="text-muted-foreground">Not configured</span>
                        )}
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">
                          {row.source === 'builtin' ? 'Built-in' : 'Package'}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Badge variant={row.status === 'trading' ? 'default' : 'outline'}>
                          {STRATEGY_STATUS_LABEL[row.status]}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        {row.last_run ? (
                          <Link
                            to={`/research/runs/${row.last_run.id}`}
                            className="hover:underline"
                          >
                            {row.last_run.net_pnl == null
                              ? row.last_run.status === 'FAILED'
                                ? 'Failed'
                                : '-'
                              : formatRupees(row.last_run.net_pnl)}{' '}
                            <span className="text-xs text-muted-foreground">
                              {row.last_run.environment === 'BACKTEST' ? 'backtest' : 'Sandbox'}
                            </span>
                          </Link>
                        ) : (
                          <span className="text-muted-foreground">Never run</span>
                        )}
                      </TableCell>
                      <TableCell>
                        <StrategyActions strategy={row} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Experiments</CardTitle>
          <CardDescription>
            An experiment holds scenarios that run on the same market data, so their results can be
            compared side by side.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              create.mutate()
            }}
          >
            <Input
              aria-label="New experiment name"
              placeholder="New experiment name"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <Input
              aria-label="What it should answer"
              placeholder="What it should answer (optional)"
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
            />
            <Button type="submit" disabled={create.isPending || !name.trim()}>
              Create
            </Button>
          </form>
          {experiments.error ? (
            <p className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
              {(experiments.error as Error).message}
            </p>
          ) : (experiments.data ?? []).length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">No experiments yet.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Experiment</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Scenarios</TableHead>
                  <TableHead>Runs</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(experiments.data ?? []).map((exp) => (
                  <TableRow key={exp.id}>
                    <TableCell className="font-medium">
                      <Link to={`/research/experiments/${exp.id}`} className="hover:underline">
                        {exp.name}
                      </Link>
                      {exp.objective && (
                        <p className="text-xs text-muted-foreground">{exp.objective}</p>
                      )}
                    </TableCell>
                    <TableCell>{EXPERIMENT_STATUS_LABEL[exp.status]}</TableCell>
                    <TableCell>{exp.scenarios}</TableCell>
                    <TableCell className="space-x-1">
                      {Object.entries(exp.runs).map(([status, count]) => (
                        <Badge key={status} variant="outline">
                          {count} {status.toLowerCase()}
                        </Badge>
                      ))}
                    </TableCell>
                    <TableCell>{exp.created_at?.slice(0, 16).replace('T', ' ')}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <LivePanel />

      <OperationsPanel />

      <Card>
        <CardHeader>
          <CardTitle>Deployments</CardTitle>
          <CardDescription>
            Scenarios trading live market data each scheduled day: in the Sandbox (OpenAlgo's
            sandbox fills every order), in Shadow (filled inside the session, nothing sent) or live
            (real orders to your broker). Deploy from a scenario on its experiment page, or from a
            strategy's row.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {deployments.error ? (
            <p className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
              {(deployments.error as Error).message}
            </p>
          ) : (deployments.data ?? []).length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">Nothing deployed yet.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Deployment</TableHead>
                  <TableHead>Where</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Schedule (IST)</TableHead>
                  <TableHead>Latest session</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(deployments.data ?? []).map((d) => (
                  <TableRow key={d.id}>
                    <TableCell className="font-medium">
                      <Link to={`/research/deployments/${d.id}`} className="hover:underline">
                        {d.name}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <Badge variant={d.environment === 'LIVE' ? 'destructive' : 'outline'}>
                        {ENVIRONMENT_LABEL[d.environment]}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant={d.status === 'ACTIVE' ? 'default' : 'secondary'}>
                        {d.status === 'ACTIVE' ? 'Active' : 'Stopped'}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {d.schedule_start}-{d.schedule_stop}, {d.schedule_days.join(' ')}
                    </TableCell>
                    <TableCell>
                      {d.latest_session ? (
                        <Link
                          to={`/research/sessions/${d.latest_session.id}`}
                          className="hover:underline"
                        >
                          {d.latest_session.trading_date}:{' '}
                          {SESSION_STATUS_LABEL[d.latest_session.status]}
                          {d.latest_session.metrics
                            ? `, ${formatRupees(d.latest_session.metrics.net_pnl)}`
                            : ''}
                        </Link>
                      ) : (
                        '-'
                      )}
                    </TableCell>
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
