// pages/research/ExperimentDetail.tsx
// One experiment: its scenarios, their runs, a side-by-side comparison of the
// latest completed run of each, and the form that adds a scenario.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  ACTIVE_STATUSES,
  COST_PRESETS,
  cancelRun,
  createScenario,
  type ExperimentDetail,
  formatNumber,
  formatPct,
  formatRupees,
  getExperiment,
  getStrategy,
  listStrategies,
  type ResearchRun,
  researchQueryKeys,
  runExperiment,
  runScenario,
  type Scenario,
  type ScenarioMode,
  shortHash,
} from '@/api/research'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { showToast } from '@/utils/toast'
import { DeployToSandbox } from './DeployToSandbox'

const STATUS_LABEL: Record<ResearchRun['status'], string> = {
  QUEUED: 'Queued',
  RUNNING: 'Running',
  COMPLETED: 'Completed',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
}

export function StatusBadge({ status }: { status: ResearchRun['status'] }) {
  const variant =
    status === 'FAILED' ? 'destructive' : status === 'COMPLETED' ? 'default' : 'secondary'
  return <Badge variant={variant}>{STATUS_LABEL[status]}</Badge>
}

function anyActive(data: ExperimentDetail | undefined): boolean {
  return !!data?.scenarios.some((s) => s.runs?.some((r) => ACTIVE_STATUSES.includes(r.status)))
}

export default function ResearchExperimentDetail() {
  const { experimentId } = useParams<{ experimentId: string }>()
  const id = Number(experimentId)
  const queryClient = useQueryClient()

  const { data, isLoading, error } = useQuery({
    queryKey: researchQueryKeys.experiment(id),
    queryFn: () => getExperiment(id),
    enabled: Number.isFinite(id),
    refetchInterval: (query) => (anyActive(query.state.data) ? 2000 : false),
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: researchQueryKeys.all })
  const runAll = useMutation({
    mutationFn: () => runExperiment(id),
    onSuccess: refresh,
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

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <Link to="/research" className="text-sm text-muted-foreground hover:underline">
            Research
          </Link>
          <h1 className="text-2xl font-bold tracking-tight">{data.name}</h1>
          {data.description && <p className="text-sm text-muted-foreground">{data.description}</p>}
        </div>
        <Button
          onClick={() => runAll.mutate()}
          disabled={runAll.isPending || data.scenarios.length === 0}
        >
          Run all scenarios
        </Button>
      </div>

      <Comparison scenarios={data.scenarios} />
      <IsolatedVsShared scenarios={data.scenarios} />

      {data.scenarios.map((scenario) => (
        <ScenarioCard key={scenario.id} scenario={scenario} onChange={refresh} />
      ))}

      <AddScenario experimentId={id} onSaved={refresh} />
    </div>
  )
}

function latestCompleted(scenario: Scenario): ResearchRun | undefined {
  return scenario.runs?.find((r) => r.status === 'COMPLETED')
}

function Comparison({ scenarios }: { scenarios: Scenario[] }) {
  const rows = scenarios
    .map((s) => ({ scenario: s, run: latestCompleted(s) }))
    .filter((row): row is { scenario: Scenario; run: ResearchRun } => !!row.run?.metrics)
  if (rows.length === 0) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>Comparison</CardTitle>
        <CardDescription>
          The latest completed run of each scenario. Returns are on each scenario's own capital.
          Annual return is shown only for windows of a year or more.
        </CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Scenario</TableHead>
              <TableHead>Mode</TableHead>
              <TableHead className="text-right">Capital</TableHead>
              <TableHead className="text-right">Net P&amp;L</TableHead>
              <TableHead className="text-right">Return</TableHead>
              <TableHead className="text-right">Annual</TableHead>
              <TableHead className="text-right">Max DD</TableHead>
              <TableHead className="text-right">Sharpe</TableHead>
              <TableHead className="text-right">Trades</TableHead>
              <TableHead className="text-right">Costs</TableHead>
              <TableHead className="text-right">Avg exposure</TableHead>
              <TableHead className="text-right">Buying power used</TableHead>
              <TableHead className="text-right">Saved by netting</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map(({ scenario, run }) => {
              const m = run.metrics as NonNullable<ResearchRun['metrics']>
              return (
                <TableRow key={scenario.id}>
                  <TableCell className="font-medium">
                    <Link to={`/research/runs/${run.id}`} className="hover:underline">
                      {scenario.name}
                    </Link>
                  </TableCell>
                  <TableCell>{scenario.mode === 'SHARED' ? 'Shared' : 'Isolated'}</TableCell>
                  <TableCell className="text-right">{formatRupees(m.capital)}</TableCell>
                  <TableCell className="text-right">{formatRupees(m.net_pnl)}</TableCell>
                  <TableCell className="text-right">{formatPct(m.return_pct)}</TableCell>
                  <TableCell className="text-right">{formatPct(m.cagr_pct)}</TableCell>
                  <TableCell className="text-right">{formatPct(m.max_drawdown_pct)}</TableCell>
                  <TableCell className="text-right">{formatNumber(m.sharpe)}</TableCell>
                  <TableCell className="text-right">{m.trades}</TableCell>
                  <TableCell className="text-right">{formatRupees(m.costs)}</TableCell>
                  <TableCell className="text-right">
                    {formatPct(m.avg_exposure_pct ?? null, 1)}
                  </TableCell>
                  <TableCell className="text-right">
                    {formatPct(m.capital_used_pct ?? null, 1)}
                  </TableCell>
                  <TableCell className="text-right">
                    {m.together && scenario.mode === 'SHARED'
                      ? formatRupees(m.together.netting.costs_saved)
                      : '-'}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

/** The same strategies and configurations, in the same order. */
function lineup(scenario: Scenario): string {
  return scenario.instances.map((i) => `${i.strategy_version.id}:${i.config_version.id}`).join('|')
}

/**
 * Pairs of scenarios that differ only in mode. Isolated answers which strategy
 * does better on its own; shared answers what happens when they trade together
 * (PRD section 60). The difference between the two is what sharing one
 * portfolio did.
 */
function IsolatedVsShared({ scenarios }: { scenarios: Scenario[] }) {
  const done = scenarios
    .map((s) => ({ scenario: s, run: latestCompleted(s) }))
    .filter((row): row is { scenario: Scenario; run: ResearchRun } => !!row.run?.metrics)
  const pairs = done
    .filter((row) => row.scenario.mode === 'ISOLATED' && row.scenario.instances.length > 1)
    .flatMap((isolated) =>
      done
        .filter(
          (shared) =>
            shared.scenario.mode === 'SHARED' &&
            lineup(shared.scenario) === lineup(isolated.scenario)
        )
        .map((shared) => ({ isolated, shared }))
    )
  if (pairs.length === 0) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>Isolated against shared</CardTitle>
        <CardDescription>
          The same strategies and configurations, alone and together. Isolated shows which does
          better on its own; shared shows what happens when they trade as one portfolio.
        </CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Isolated</TableHead>
              <TableHead>Shared</TableHead>
              <TableHead className="text-right">Net P&amp;L alone</TableHead>
              <TableHead className="text-right">Net P&amp;L together</TableHead>
              <TableHead className="text-right">Difference</TableHead>
              <TableHead className="text-right">Costs alone</TableHead>
              <TableHead className="text-right">Costs together</TableHead>
              <TableHead className="text-right">Max DD alone</TableHead>
              <TableHead className="text-right">Max DD together</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {pairs.map(({ isolated, shared }) => {
              const a = isolated.run.metrics as NonNullable<ResearchRun['metrics']>
              const b = shared.run.metrics as NonNullable<ResearchRun['metrics']>
              return (
                <TableRow key={`${isolated.scenario.id}-${shared.scenario.id}`}>
                  <TableCell>
                    <Link to={`/research/runs/${isolated.run.id}`} className="hover:underline">
                      {isolated.scenario.name}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Link to={`/research/runs/${shared.run.id}`} className="hover:underline">
                      {shared.scenario.name}
                    </Link>
                  </TableCell>
                  <TableCell className="text-right">{formatRupees(a.net_pnl)}</TableCell>
                  <TableCell className="text-right">{formatRupees(b.net_pnl)}</TableCell>
                  <TableCell className="text-right">
                    {formatRupees(b.net_pnl - a.net_pnl)}
                  </TableCell>
                  <TableCell className="text-right">{formatRupees(a.costs)}</TableCell>
                  <TableCell className="text-right">{formatRupees(b.costs)}</TableCell>
                  <TableCell className="text-right">{formatPct(a.max_drawdown_pct)}</TableCell>
                  <TableCell className="text-right">{formatPct(b.max_drawdown_pct)}</TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

function limitsText(scenario: Scenario): string {
  const policy = scenario.risk_policy
  const parts = []
  if (policy.daily_loss_limit)
    parts.push(`daily loss limit ${formatRupees(policy.daily_loss_limit)}`)
  if (policy.daily_profit_target)
    parts.push(`daily profit target ${formatRupees(policy.daily_profit_target)}`)
  if (policy.lock_profit_at) parts.push(`profit locked from ${formatRupees(policy.lock_profit_at)}`)
  if (parts.length === 0) return ''
  const scope = scenario.mode === 'SHARED' ? 'for the portfolio' : 'for each strategy'
  return ` ${parts.join(', ')} ${scope}.`
}

function ScenarioCard({ scenario, onChange }: { scenario: Scenario; onChange: () => void }) {
  const run = useMutation({
    mutationFn: () => runScenario(scenario.id),
    onSuccess: ({ created, item }) => {
      if (!created && item.status === 'COMPLETED') {
        showToast.info(`Run #${item.id} already has the result for exactly these inputs`)
      }
      onChange()
    },
    onError: (err: Error) => showToast.error(err.message),
  })
  const cancel = useMutation({
    mutationFn: (runId: number) => cancelRun(runId),
    onSuccess: onChange,
    onError: (err: Error) => showToast.error(err.message),
  })
  const spec = scenario.dataset_spec
  const active = scenario.runs?.find((r) => ACTIVE_STATUSES.includes(r.status))
  const [deploying, setDeploying] = useState(false)

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <div>
          <CardTitle className="flex items-center gap-2">
            {scenario.name}
            <Badge variant="outline">{scenario.mode === 'SHARED' ? 'Shared' : 'Isolated'}</Badge>
          </CardTitle>
          <CardDescription>
            {spec.interval} bars, {spec.symbols.length} symbols on {spec.exchange}, {spec.start} to{' '}
            {spec.end}.{' '}
            {COST_PRESETS.find((p) => p.value === scenario.cost_model.preset)?.label ??
              scenario.cost_model.preset}
            , slippage {scenario.cost_model.slippage_bps} bps.
            {scenario.mode === 'SHARED' &&
              ` Shared capital ${
                scenario.shared_capital
                  ? formatRupees(scenario.shared_capital)
                  : 'is the total of the strategies'
              }, exposure limit ${scenario.risk_policy.max_gross_leverage}x.`}
            {limitsText(scenario)}
          </CardDescription>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => setDeploying(true)}>
            Deploy to Sandbox
          </Button>
          {active ? (
            <Button variant="outline" onClick={() => cancel.mutate(active.id)}>
              Cancel
            </Button>
          ) : (
            <Button onClick={() => run.mutate()} disabled={run.isPending}>
              Run
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <ul className="space-y-1 text-sm">
          {scenario.instances.map((inst) => (
            <li key={inst.position}>
              <span className="font-medium">{inst.strategy_key}</span>{' '}
              <span className="font-mono text-xs text-muted-foreground">
                {shortHash(inst.strategy_version.version_hash)}
              </span>{' '}
              config v{inst.config_version.version_no},{' '}
              {formatRupees(inst.config_version.config.capital)},{' '}
              {inst.config_version.config.leverage}x
            </li>
          ))}
        </ul>
        {(scenario.runs ?? []).length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Progress</TableHead>
                <TableHead>Result</TableHead>
                <TableHead>Queued</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(scenario.runs ?? []).map((r) => (
                <TableRow key={r.id}>
                  <TableCell>
                    <Link to={`/research/runs/${r.id}`} className="hover:underline">
                      #{r.id}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={r.status} />
                  </TableCell>
                  <TableCell className="w-48">
                    {ACTIVE_STATUSES.includes(r.status) ? (
                      <Progress value={Math.round(r.progress * 100)} />
                    ) : null}
                  </TableCell>
                  <TableCell className="max-w-md text-sm">
                    {r.metrics
                      ? `${formatRupees(r.metrics.net_pnl)} (${formatPct(r.metrics.return_pct)}), ${r.metrics.trades} trades`
                      : (r.message ?? '')}
                  </TableCell>
                  <TableCell>{r.queued_at?.slice(0, 16).replace('T', ' ')}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
      {deploying && (
        <DeployToSandbox scenario={scenario} open={deploying} onOpenChange={setDeploying} />
      )}
    </Card>
  )
}

interface InstanceDraft {
  strategyId: number | null
  versionId: number | null
  configId: number | null
}

function InstanceRow({
  draft,
  onChange,
  onRemove,
}: {
  draft: InstanceDraft
  onChange: (draft: InstanceDraft) => void
  onRemove: () => void
}) {
  const strategies = useQuery({ queryKey: researchQueryKeys.strategies(), queryFn: listStrategies })
  const detail = useQuery({
    queryKey: researchQueryKeys.strategy(draft.strategyId ?? -1),
    queryFn: () => getStrategy(draft.strategyId as number),
    enabled: draft.strategyId !== null,
  })
  return (
    <div className="grid gap-2 sm:grid-cols-[1fr_1fr_1fr_auto]">
      <Select
        value={draft.strategyId === null ? '' : String(draft.strategyId)}
        onValueChange={(v) => onChange({ strategyId: Number(v), versionId: null, configId: null })}
      >
        <SelectTrigger aria-label="Strategy">
          <SelectValue placeholder="Strategy" />
        </SelectTrigger>
        <SelectContent>
          {(strategies.data?.items ?? []).map((s) => (
            <SelectItem key={s.id} value={String(s.id)}>
              {s.key}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select
        value={draft.versionId === null ? '' : String(draft.versionId)}
        onValueChange={(v) => onChange({ ...draft, versionId: Number(v) })}
        disabled={!detail.data}
      >
        <SelectTrigger aria-label="Version">
          <SelectValue placeholder="Version" />
        </SelectTrigger>
        <SelectContent>
          {(detail.data?.versions ?? []).map((v) => (
            <SelectItem key={v.id} value={String(v.id)}>
              {v.dist_version} / {shortHash(v.version_hash)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select
        value={draft.configId === null ? '' : String(draft.configId)}
        onValueChange={(v) => onChange({ ...draft, configId: Number(v) })}
        disabled={!detail.data}
      >
        <SelectTrigger aria-label="Configuration">
          <SelectValue placeholder="Configuration" />
        </SelectTrigger>
        <SelectContent>
          {(detail.data?.configs ?? []).map((c) => (
            <SelectItem key={c.id} value={String(c.id)}>
              v{c.version_no}: {formatRupees(c.config.capital)}, {c.config.leverage}x
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Button type="button" variant="ghost" onClick={onRemove}>
        Remove
      </Button>
    </div>
  )
}

const EMPTY_DRAFT: InstanceDraft = { strategyId: null, versionId: null, configId: null }

function AddScenario({ experimentId, onSaved }: { experimentId: number; onSaved: () => void }) {
  const [name, setName] = useState('')
  const [mode, setMode] = useState<ScenarioMode>('ISOLATED')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [preset, setPreset] = useState(COST_PRESETS[0].value)
  const [slippage, setSlippage] = useState('2')
  const [maxGross, setMaxGross] = useState('1')
  const [sharedCapital, setSharedCapital] = useState('')
  const [dailyLoss, setDailyLoss] = useState('')
  const [dailyTarget, setDailyTarget] = useState('')
  const [drafts, setDrafts] = useState<InstanceDraft[]>([EMPTY_DRAFT])

  const save = useMutation({
    mutationFn: () => {
      const incomplete = drafts.some((d) => d.versionId === null || d.configId === null)
      if (incomplete) throw new Error('Choose a strategy, version and configuration on every row.')
      return createScenario(experimentId, {
        name,
        mode,
        start,
        end,
        cost_model: { preset, slippage_bps: Number(slippage) },
        risk_policy: {
          max_gross_leverage: Number(maxGross),
          ...(dailyLoss.trim() ? { daily_loss_limit: Number(dailyLoss) } : {}),
          ...(dailyTarget.trim() ? { daily_profit_target: Number(dailyTarget) } : {}),
        },
        shared_capital: sharedCapital.trim() === '' ? null : Number(sharedCapital),
        instances: drafts.map((d) => ({
          strategy_version_id: d.versionId as number,
          config_version_id: d.configId as number,
        })),
      })
    },
    onSuccess: () => {
      showToast.success('Scenario added')
      setName('')
      setDrafts([EMPTY_DRAFT])
      onSaved()
    },
    onError: (err: Error) => showToast.error(err.message),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Add a scenario</CardTitle>
        <CardDescription>
          Isolated gives every strategy its own capital and positions. Shared puts them in one
          portfolio, nets their orders, and keeps each strategy's own P&amp;L. A scenario cannot be
          edited once saved; add another to try different settings.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            save.mutate()
          }}
        >
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div className="space-y-1.5">
              <Label htmlFor="sc-name">Name</Label>
              <Input id="sc-name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>Mode</Label>
              <Select value={mode} onValueChange={(v) => setMode(v as ScenarioMode)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="ISOLATED">Isolated</SelectItem>
                  <SelectItem value="SHARED">Shared</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="sc-start">From</Label>
              <Input
                id="sc-start"
                type="date"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="sc-end">To</Label>
              <Input id="sc-end" type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>Costs</Label>
              <Select value={preset} onValueChange={setPreset}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {COST_PRESETS.map((p) => (
                    <SelectItem key={p.value} value={p.value}>
                      {p.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="sc-slip">Slippage (bps per order)</Label>
              <Input
                id="sc-slip"
                inputMode="decimal"
                value={slippage}
                onChange={(e) => setSlippage(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="sc-loss">
                Daily loss limit (Rs, {mode === 'SHARED' ? 'portfolio' : 'each strategy'})
              </Label>
              <Input
                id="sc-loss"
                inputMode="decimal"
                placeholder="None"
                value={dailyLoss}
                onChange={(e) => setDailyLoss(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="sc-target">
                Daily profit target (Rs, {mode === 'SHARED' ? 'portfolio' : 'each strategy'})
              </Label>
              <Input
                id="sc-target"
                inputMode="decimal"
                placeholder="None"
                value={dailyTarget}
                onChange={(e) => setDailyTarget(e.target.value)}
              />
            </div>
            {mode === 'SHARED' && (
              <>
                <div className="space-y-1.5">
                  <Label htmlFor="sc-cap">Shared capital (Rs)</Label>
                  <Input
                    id="sc-cap"
                    inputMode="decimal"
                    placeholder="Total of the strategies"
                    value={sharedCapital}
                    onChange={(e) => setSharedCapital(e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="sc-gross">Exposure limit (x capital)</Label>
                  <Input
                    id="sc-gross"
                    inputMode="decimal"
                    value={maxGross}
                    onChange={(e) => setMaxGross(e.target.value)}
                  />
                </div>
              </>
            )}
          </div>
          <div className="space-y-2">
            <Label>Strategies</Label>
            {drafts.map((draft, n) => (
              <InstanceRow
                key={n}
                draft={draft}
                onChange={(next) => setDrafts((all) => all.map((d, i) => (i === n ? next : d)))}
                onRemove={() => setDrafts((all) => all.filter((_, i) => i !== n))}
              />
            ))}
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setDrafts((all) => [...all, EMPTY_DRAFT])}
            >
              Add strategy
            </Button>
          </div>
          <Button type="submit" disabled={save.isPending || drafts.length === 0}>
            Save scenario
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}
