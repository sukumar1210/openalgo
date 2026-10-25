// pages/runs/RunForm.tsx
// Define a Run, or edit one: its money, how each day's result is handled, the
// most a strategy may hold, and the strategies in it with their allocations.
// The previews below the form are worked out with the rules the Run trades by
// (portfolio/run_capital.py), so what is shown is what will happen.

import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { pythonStrategyApi } from '@/api/python-strategy'
import {
  type AllocationKind,
  actOnRun,
  createRun,
  editRun,
  errorMessage,
  getAccounts,
  getRun,
  POLICY_LABEL,
  type RunDefinition,
  type RunLimit,
  type RunMode,
  type RunPolicy,
  runKeys,
  type Sizing,
} from '@/api/runs'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn } from '@/lib/utils'
import type { PythonStrategy } from '@/types/python-strategy'
import { RunsNav, tone, useMoney } from './shared'

/** A source trades at least this, so members scale down from it (run_service.REFERENCE_FLOOR). */
const REFERENCE_FLOOR = 10_000_000
const EXCHANGES = ['NSE', 'BSE']

interface MemberRow {
  key: number
  script_id: string
  label: string
  allocation_kind: AllocationKind
  allocation_value: string
  sizing: Sizing
}

const CHOICES = {
  mode: [
    ['isolated', 'Isolated', 'Each strategy trades an account of its own. Its money is its own.'],
    [
      'shared',
      'Shared',
      "One account for the whole Run. Positions net; each strategy's share is read from its order name.",
    ],
  ],
  policy: [
    [
      'reset',
      'Reset daily',
      'Every day starts from the allocation. A profit is swept out, a loss topped up.',
    ],
    [
      'own',
      'Own result',
      'A winner keeps its gains, a loser its losses; neither touches the other. Isolated only.',
    ],
    ['pooled', 'Pooled', "The day's balances are added up and shared out again by allocation."],
  ],
  limit: [
    ['capital', 'Its capital that day', 'Grows with its profits and shrinks with its losses.'],
    ['fixed', 'A fixed amount', 'Its allocation every day, whatever its account holds.'],
  ],
} as const

export function allocationOf(capital: number, kind: AllocationKind, value: number): number {
  return kind === 'fixed' ? value : (capital * value) / 100
}

/** Day two's capital by each policy, given day one's results (run_capital.day_open). */
export function dayTwo(allocations: number[], results: number[]) {
  const closing = allocations.map((a, i) => a + (results[i] ?? 0))
  const total = closing.reduce((t, v) => t + v, 0)
  const weight = allocations.reduce((t, v) => t + v, 0)
  return {
    reset: allocations,
    own: closing,
    pooled: allocations.map((a) => (weight ? (total * a) / weight : 0)),
  }
}

function strategyProblem(s: PythonStrategy): string | null {
  if (!EXCHANGES.includes((s.exchange || 'NSE').toUpperCase()))
    return `trades ${s.exchange}: Runs take NSE and BSE strategies for now`
  if (!s.schedule_start_time) return 'give it a schedule on its Schedule tab first'
  return null
}

export default function RunForm() {
  const { runId } = useParams()
  const editing = runId ? Number(runId) : null
  const navigate = useNavigate()
  const money = useMoney()
  const strategies = useQuery({
    queryKey: ['python-strategies'],
    queryFn: pythonStrategyApi.getStrategies,
  })
  const accounts = useQuery({
    queryKey: runKeys.accounts(),
    queryFn: getAccounts,
    enabled: !editing,
  })
  const existing = useQuery({
    queryKey: runKeys.run(editing ?? 0),
    queryFn: () => getRun(editing ?? 0),
    enabled: editing !== null,
  })

  const [name, setName] = useState('')
  const [capital, setCapital] = useState('')
  const [mode, setMode] = useState<RunMode>('isolated')
  const [policy, setPolicy] = useState<RunPolicy>('own')
  const [limit, setLimit] = useState<RunLimit>('capital')
  const [lossLimit, setLossLimit] = useState('')
  const [members, setMembers] = useState<MemberRow[]>([])
  const [nextKey, setNextKey] = useState(1)
  const [results, setResults] = useState<Record<number, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  // A new Run starts at the Sandbox's capital; an edit starts from what it is.
  useEffect(() => {
    if (!editing && accounts.data && capital === '')
      setCapital(String(accounts.data.main.starting_capital))
  }, [editing, accounts.data, capital])
  useEffect(() => {
    const run = existing.data
    if (!run) return
    const v = run.upcoming ?? run.version
    setName(run.name)
    setCapital(String(v.capital))
    setMode(v.mode)
    setPolicy(v.policy)
    setLimit(v.limit)
    setLossLimit(v.loss_limit_pct == null ? '' : String(v.loss_limit_pct))
    setMembers(
      v.members.map((m, i) => ({
        key: i + 1,
        script_id: m.script_id,
        label: m.label,
        allocation_kind: m.allocation_kind,
        allocation_value: String(m.allocation_value),
        sizing: m.sizing,
      }))
    )
    setNextKey(v.members.length + 1)
  }, [existing.data])

  const capitalValue = Number(capital) || 0
  const byId = useMemo(
    () => new Map((strategies.data ?? []).map((s) => [s.id, s])),
    [strategies.data]
  )
  const allocations = members.map((m) =>
    allocationOf(capitalValue, m.allocation_kind, Number(m.allocation_value) || 0)
  )
  const allocated = allocations.reduce((t, a) => t + a, 0)
  const resultsList = members.map((m, i) =>
    results[m.key] === undefined
      ? Math.round(allocations[i] * (i === 0 ? 0.02 : -0.04))
      : Number(results[m.key]) || 0
  )
  const twos = dayTwo(allocations, resultsList)

  const update = (key: number, patch: Partial<MemberRow>) =>
    setMembers((rows) => rows.map((r) => (r.key === key ? { ...r, ...patch } : r)))
  const add = () => {
    const usable = (strategies.data ?? []).find(
      (s) => !strategyProblem(s) && !members.some((m) => m.script_id === s.id)
    )
    setMembers((rows) => [
      ...rows,
      {
        key: nextKey,
        script_id: usable?.id ?? '',
        label: '',
        allocation_kind: 'fixed',
        allocation_value: '',
        sizing: 'platform',
      },
    ])
    setNextKey((k) => k + 1)
  }

  const definition = (): RunDefinition => ({
    name: name.trim(),
    capital: capitalValue,
    mode,
    policy,
    limit,
    loss_limit_pct: lossLimit === '' ? null : Number(lossLimit),
    members: members.map((m) => ({
      script_id: m.script_id,
      label: m.label.trim() || undefined,
      allocation_kind: m.allocation_kind,
      allocation_value: Number(m.allocation_value) || 0,
      sizing: m.sizing,
    })),
  })

  const save = async (start: boolean) => {
    setError(null)
    setSaving(true)
    try {
      const run = editing ? await editRun(editing, definition()) : await createRun(definition())
      if (start) await actOnRun(run.id, 'start')
      navigate(`/runs/${run.id}`)
    } catch (e) {
      setError(errorMessage(e, 'The Run could not be saved just now. Try again.'))
    } finally {
      setSaving(false)
    }
  }

  const trading = existing.data && existing.data.status !== 'draft'

  return (
    <div className="container mx-auto max-w-5xl space-y-6 px-4 py-6">
      <RunsNav />
      <div>
        <Link to={editing ? `/runs/${editing}` : '/runs'} className="text-sm hover:underline">
          {editing ? (existing.data?.name ?? 'Run') : 'Runs'}
        </Link>
        <h1 className="text-2xl font-bold tracking-tight">{editing ? 'Edit Run' : 'New Run'}</h1>
        <p className="text-muted-foreground">
          {trading
            ? 'An edit applies from the next trading day. Today finishes on what it started with.'
            : "A scenario to trade in the Sandbox: which strategies, how much money, and what happens to each day's result."}
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>The Run</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-3">
          <div className="space-y-1.5">
            <Label htmlFor="run-name">Name</Label>
            <Input
              id="run-name"
              value={name}
              maxLength={60}
              disabled={editing !== null}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="run-capital">Capital</Label>
            <Input
              id="run-capital"
              type="number"
              min={1}
              value={capital}
              onChange={(e) => setCapital(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Starts at your Sandbox capital; change it freely.
            </p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="run-loss">Daily loss limit, % of each account's capital</Label>
            <Input
              id="run-loss"
              type="number"
              min={0}
              max={100}
              step="0.1"
              placeholder="None"
              value={lossLimit}
              onChange={(e) => setLossLimit(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Past it, that account's intraday positions are squared off for the day.
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>How its money works</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          <ChoiceGroup
            legend="Mode"
            options={CHOICES.mode}
            value={mode}
            disabled={(v) => editing !== null && v !== mode}
            onChange={(v) => {
              setMode(v as RunMode)
              if (v === 'shared' && policy === 'own') setPolicy('pooled')
            }}
            note={editing !== null ? 'A Run keeps its mode for its whole life.' : undefined}
          />
          <ChoiceGroup
            legend="Each day's profit and loss"
            options={CHOICES.policy}
            value={policy}
            disabled={(v) => v === 'own' && mode === 'shared'}
            onChange={(v) => setPolicy(v as RunPolicy)}
          />
          <ChoiceGroup
            legend="Most a strategy may hold, across every symbol"
            options={CHOICES.limit}
            value={limit}
            onChange={(v) => setLimit(v as RunLimit)}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Strategies</CardTitle>
          <CardDescription>
            Each is frozen on the version of its script in Strategies today.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4 overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Strategy</TableHead>
                <TableHead>Label</TableHead>
                <TableHead>Allocation</TableHead>
                <TableHead>Sized by</TableHead>
                <TableHead>
                  <span className="sr-only">Remove</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {members.map((m, i) => {
                const s = byId.get(m.script_id)
                return (
                  <TableRow key={m.key}>
                    <TableCell className="min-w-56">
                      <select
                        aria-label={`Strategy ${i + 1}`}
                        value={m.script_id}
                        onChange={(e) => update(m.key, { script_id: e.target.value })}
                        className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm"
                      >
                        <option value="">Choose a strategy</option>
                        {(strategies.data ?? []).map((option) => {
                          const problem = strategyProblem(option)
                          return (
                            <option key={option.id} value={option.id} disabled={problem !== null}>
                              {option.name}
                              {problem ? ` (${problem})` : ''}
                            </option>
                          )
                        })}
                      </select>
                      {s && (
                        <p className="mt-1 text-xs text-muted-foreground">
                          {(s.exchange || 'NSE').toUpperCase()} · {s.schedule_start_time} to{' '}
                          {s.schedule_stop_time ?? 'the close'}
                          {s.symbol_list ? ` · ${s.symbol_list.count} symbols` : ''}
                        </p>
                      )}
                    </TableCell>
                    <TableCell>
                      <Input
                        aria-label={`Label ${i + 1}`}
                        placeholder={s?.name ?? ''}
                        value={m.label}
                        maxLength={60}
                        onChange={(e) => update(m.key, { label: e.target.value })}
                      />
                    </TableCell>
                    <TableCell className="min-w-56">
                      <div className="flex gap-2">
                        <select
                          aria-label={`Allocation kind ${i + 1}`}
                          value={m.allocation_kind}
                          onChange={(e) =>
                            update(m.key, { allocation_kind: e.target.value as AllocationKind })
                          }
                          className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
                        >
                          <option value="fixed">Rupees</option>
                          <option value="percent">Share of the Run, %</option>
                        </select>
                        <Input
                          aria-label={`Allocation ${i + 1}`}
                          type="number"
                          min={0}
                          value={m.allocation_value}
                          onChange={(e) => update(m.key, { allocation_value: e.target.value })}
                        />
                      </div>
                    </TableCell>
                    <TableCell>
                      <select
                        aria-label={`Sized by ${i + 1}`}
                        value={m.sizing}
                        onChange={(e) => update(m.key, { sizing: e.target.value as Sizing })}
                        className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
                      >
                        <option value="platform">Its capital</option>
                        <option value="self">Its own quantity</option>
                      </select>
                    </TableCell>
                    <TableCell>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setMembers((rows) => rows.filter((r) => r.key !== m.key))}
                      >
                        Remove
                      </Button>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
          <Button variant="outline" onClick={add}>
            Add a strategy
          </Button>
          {members.length > 0 && (
            <div className="space-y-1.5">
              <div className="flex justify-between text-sm">
                <span>Allocated</span>
                <span className="tabular-nums">
                  {money(allocated)} of {money(capitalValue)}
                  {capitalValue ? ` · ${((allocated / capitalValue) * 100).toFixed(0)}%` : ''}
                </span>
              </div>
              <Progress
                value={capitalValue ? Math.min((allocated / capitalValue) * 100, 100) : 0}
              />
              <p className="text-xs text-muted-foreground">
                Allocations may add up to less or more than the Run's capital; returns are measured
                on what was allocated.
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      {members.length > 0 && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Day one</CardTitle>
              <CardDescription>
                Each strategy runs once for every Run that holds it with these settings; this Run
                copies its trades scaled to its own capital.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Strategy</TableHead>
                    <TableHead className="text-right">Capital</TableHead>
                    <TableHead className="text-right">Limit</TableHead>
                    <TableHead className="text-right">Copies at</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {members.map((m, i) => {
                    const reference = Math.max(REFERENCE_FLOOR, allocations[i])
                    return (
                      <TableRow key={m.key}>
                        <TableCell>{memberName(m, byId)}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {money(allocations[i])}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {money(allocations[i])}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {m.sizing === 'self'
                            ? 'its own quantity'
                            : `${((allocations[i] / reference) * 100).toFixed(2)}%`}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
              {mode === 'shared' && (
                <p className="text-xs text-muted-foreground">
                  One account holds {money(allocated)} for all of them.
                </p>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Day two opens with</CardTitle>
              <CardDescription>If day one ends with these results:</CardDescription>
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead />
                    {members.map((m) => (
                      <TableHead key={m.key} className="text-right">
                        {memberName(m, byId)}
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  <TableRow>
                    <TableCell className="text-muted-foreground">Day one's result</TableCell>
                    {members.map((m, i) => (
                      <TableCell key={m.key} className="text-right">
                        <Input
                          aria-label={`Day one result ${i + 1}`}
                          type="number"
                          className={cn('ml-auto w-28 text-right', tone(resultsList[i]))}
                          value={results[m.key] ?? String(resultsList[i])}
                          onChange={(e) => setResults((r) => ({ ...r, [m.key]: e.target.value }))}
                        />
                      </TableCell>
                    ))}
                  </TableRow>
                  {(['reset', 'own', 'pooled'] as const)
                    .filter((p) => !(p === 'own' && mode === 'shared'))
                    .map((p) => (
                      <TableRow key={p} className={cn(p === policy && 'bg-muted/50 font-medium')}>
                        <TableCell>
                          {POLICY_LABEL[p]}
                          {p === policy ? ' (chosen)' : ''}
                        </TableCell>
                        {members.map((m, i) => (
                          <TableCell key={m.key} className="text-right tabular-nums">
                            {money(twos[p][i])}
                          </TableCell>
                        ))}
                      </TableRow>
                    ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </div>
      )}

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <div className="flex flex-wrap justify-end gap-2">
        <Button variant="outline" asChild>
          <Link to={editing ? `/runs/${editing}` : '/runs'}>Cancel</Link>
        </Button>
        {editing ? (
          <Button disabled={saving} onClick={() => save(false)}>
            Save the edit
          </Button>
        ) : (
          <>
            <Button variant="outline" disabled={saving} onClick={() => save(false)}>
              Save as draft
            </Button>
            <Button disabled={saving} onClick={() => save(true)}>
              Save and start
            </Button>
          </>
        )}
      </div>
    </div>
  )
}

function memberName(m: MemberRow, byId: Map<string, PythonStrategy>): string {
  return m.label.trim() || byId.get(m.script_id)?.name || 'Strategy'
}

function ChoiceGroup({
  legend,
  options,
  value,
  onChange,
  disabled,
  note,
}: {
  legend: string
  options: readonly (readonly [string, string, string])[]
  value: string
  onChange: (value: string) => void
  disabled?: (value: string) => boolean
  note?: string
}) {
  return (
    <fieldset className="space-y-2">
      <legend className="text-sm font-medium">{legend}</legend>
      <div className="grid gap-2 sm:grid-cols-3">
        {options.map(([key, title, text]) => {
          const off = disabled?.(key) ?? false
          return (
            <label
              key={key}
              className={cn(
                'flex cursor-pointer gap-2 rounded-lg border p-3 text-sm',
                value === key ? 'border-primary ring-1 ring-primary' : 'border-border',
                off && 'cursor-not-allowed opacity-50'
              )}
            >
              <input
                type="radio"
                name={legend}
                value={key}
                checked={value === key}
                disabled={off}
                onChange={() => onChange(key)}
                className="mt-0.5"
              />
              <span>
                <span className="block font-medium">{title}</span>
                <span className="text-muted-foreground">{text}</span>
              </span>
            </label>
          )
        })}
      </div>
      {note && <p className="text-xs text-muted-foreground">{note}</p>}
    </fieldset>
  )
}
