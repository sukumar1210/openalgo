// pages/research/StrategyDetail.tsx
// One strategy: its immutable versions, its configuration history, and the form
// that saves a new configuration version. Nothing on this page edits a version.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  type ConfigVersion,
  formatPct,
  formatRupees,
  getStrategy,
  type ResearchConfig,
  researchQueryKeys,
  saveConfig,
  shortHash,
} from '@/api/research'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
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
import { Textarea } from '@/components/ui/textarea'
import { showToast } from '@/utils/toast'

/** Form state: everything as the strings the inputs hold. */
interface ConfigForm {
  capital: string
  allocationPct: string
  leverage: string
  product: 'MIS' | 'CNC'
  entry_time: string
  exit_time: string
  stopPct: string
  targetPct: string
  universe: string
  exchange: string
  note: string
}

const DEFAULT_FORM: ConfigForm = {
  capital: '500000',
  allocationPct: '100',
  leverage: '1',
  product: 'MIS',
  entry_time: '09:15',
  exit_time: '15:10',
  stopPct: '',
  targetPct: '',
  universe: '',
  exchange: 'NSE',
  note: '',
}

function formFromConfig(config: ResearchConfig): ConfigForm {
  return {
    capital: String(config.capital),
    allocationPct: String(Math.round(config.allocation * 10000) / 100),
    leverage: String(config.leverage),
    product: config.product,
    entry_time: config.entry_time,
    exit_time: config.exit_time ?? '',
    stopPct: config.stop_pct == null ? '' : String(config.stop_pct),
    targetPct: config.target_pct == null ? '' : String(config.target_pct),
    universe: config.universe.join(', '),
    exchange: config.exchange,
    note: '',
  }
}

/** Numbers stay numbers and blanks stay null, so the server's validation speaks. */
export function configFromForm(form: ConfigForm): ResearchConfig {
  const optional = (value: string) => (value.trim() === '' ? null : Number(value))
  return {
    capital: Number(form.capital),
    allocation: Number(form.allocationPct) / 100,
    leverage: Number(form.leverage),
    product: form.product,
    entry_time: form.entry_time,
    exit_time: form.exit_time.trim() === '' ? null : form.exit_time,
    stop_pct: optional(form.stopPct),
    target_pct: optional(form.targetPct),
    universe: form.universe
      .split(/[\s,]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean),
    exchange: form.exchange,
    execution_mode: 'backtest',
  }
}

function describe(config: ResearchConfig): string {
  const parts = [
    formatRupees(config.capital),
    `${Math.round(config.allocation * 100)}%`,
    `${config.leverage}x`,
    config.product,
    `${config.entry_time}-${config.exit_time ?? 'hold'}`,
  ]
  if (config.stop_pct != null) parts.push(`stop ${config.stop_pct}%`)
  if (config.target_pct != null) parts.push(`target ${config.target_pct}%`)
  parts.push(`${config.universe.length} symbols`)
  return parts.join(', ')
}

export default function ResearchStrategyDetail() {
  const { strategyId } = useParams<{ strategyId: string }>()
  const id = Number(strategyId)
  const queryClient = useQueryClient()
  const [form, setForm] = useState<ConfigForm>(DEFAULT_FORM)
  const [baseId, setBaseId] = useState<number | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: researchQueryKeys.strategy(id),
    queryFn: () => getStrategy(id),
    enabled: Number.isFinite(id),
  })

  const latest = data?.configs[0]
  useEffect(() => {
    if (latest && baseId === null) {
      setForm(formFromConfig(latest.config))
      setBaseId(latest.id)
    }
  }, [latest, baseId])

  const save = useMutation({
    mutationFn: () => saveConfig(id, configFromForm(form), baseId, form.note),
    onSuccess: ({ item, created }) => {
      if (created) showToast.success(`Saved as configuration v${item.version_no}`)
      else showToast.info(`This configuration already exists as v${item.version_no}`)
      setBaseId(item.id)
      setForm((f) => ({ ...f, note: '' }))
      queryClient.invalidateQueries({ queryKey: researchQueryKeys.all })
    },
    onError: (err: Error) => showToast.error(err.message),
  })

  const loadInto = (row: ConfigVersion) => {
    setForm(formFromConfig(row.config))
    setBaseId(row.id)
  }

  const set = (key: keyof ConfigForm) => (value: string) => setForm((f) => ({ ...f, [key]: value }))

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
      <div>
        <Link to="/research" className="text-sm text-muted-foreground hover:underline">
          Research
        </Link>
        <h1 className="text-2xl font-bold tracking-tight">{data.key}</h1>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Versions</CardTitle>
          <CardDescription>
            Each version is the exact code and model a backtest ran with. Versions are never edited.
          </CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Package</TableHead>
                <TableHead>Version id</TableHead>
                <TableHead>Code</TableHead>
                <TableHead>Artifact</TableHead>
                <TableHead>Model</TableHead>
                <TableHead>Adapter</TableHead>
                <TableHead>Bars</TableHead>
                <TableHead>First seen</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.versions.map((v) => (
                <TableRow key={v.id}>
                  <TableCell>
                    {v.dist_name} {v.dist_version}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{shortHash(v.version_hash)}</TableCell>
                  <TableCell className="font-mono text-xs">{shortHash(v.code_hash)}</TableCell>
                  <TableCell className="font-mono text-xs">{shortHash(v.artifact_hash)}</TableCell>
                  <TableCell className="font-mono text-xs">{shortHash(v.model_hash)}</TableCell>
                  <TableCell>{v.adapter_version ?? '-'}</TableCell>
                  <TableCell>{v.interval}</TableCell>
                  <TableCell>{v.created_at?.slice(0, 16).replace('T', ' ')}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Configuration history</CardTitle>
          <CardDescription>
            Saving a change creates a new version. Earlier versions, and every run that used them,
            stay exactly as they were.
          </CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          {data.configs.length === 0 ? (
            <p className="py-4 text-sm text-muted-foreground">
              No configuration yet. Save one below.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Version</TableHead>
                  <TableHead>Hash</TableHead>
                  <TableHead>Settings</TableHead>
                  <TableHead>Based on</TableHead>
                  <TableHead>Used by</TableHead>
                  <TableHead>Note</TableHead>
                  <TableHead>Saved</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.configs.map((c) => (
                  <TableRow key={c.id}>
                    <TableCell className="font-medium">v{c.version_no}</TableCell>
                    <TableCell className="font-mono text-xs">{shortHash(c.config_hash)}</TableCell>
                    <TableCell>{describe(c.config)}</TableCell>
                    <TableCell>
                      {c.parent_id
                        ? `v${data.configs.find((p) => p.id === c.parent_id)?.version_no ?? '?'}`
                        : '-'}
                    </TableCell>
                    <TableCell>
                      {c.used_by === 1 ? '1 scenario' : `${c.used_by} scenarios`}
                    </TableCell>
                    <TableCell className="max-w-48 truncate">{c.note ?? ''}</TableCell>
                    <TableCell>{c.created_at?.slice(0, 16).replace('T', ' ')}</TableCell>
                    <TableCell className="text-right">
                      <Button variant="ghost" size="sm" onClick={() => loadInto(c)}>
                        Start from this
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {data.configs.some((c) => c.results.length > 0) && (
        <Card>
          <CardHeader>
            <CardTitle>How each configuration did</CardTitle>
            <CardDescription>
              This strategy's own result in the latest finished runs and sessions that used each
              configuration, newest first. In a shared scenario this is its share of the portfolio.
            </CardDescription>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Configuration</TableHead>
                  <TableHead>Scenario</TableHead>
                  <TableHead>Where</TableHead>
                  <TableHead className="text-right">Net P&amp;L</TableHead>
                  <TableHead className="text-right">Return</TableHead>
                  <TableHead className="text-right">Max DD</TableHead>
                  <TableHead className="text-right">Trades</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.configs.flatMap((c) =>
                  c.results.map((r) => (
                    <TableRow key={`${c.id}-${r.run_id}`}>
                      <TableCell className="font-medium">v{c.version_no}</TableCell>
                      <TableCell>
                        <Link to={`/research/runs/${r.run_id}`} className="hover:underline">
                          {r.scenario}
                        </Link>
                      </TableCell>
                      <TableCell>
                        {r.environment === 'BACKTEST'
                          ? 'Backtest'
                          : `Sandbox ${r.trading_date ?? ''}`}
                        , {r.mode === 'SHARED' ? 'shared' : 'isolated'}
                      </TableCell>
                      <TableCell className="text-right">{formatRupees(r.net_pnl)}</TableCell>
                      <TableCell className="text-right">{formatPct(r.return_pct)}</TableCell>
                      <TableCell className="text-right">{formatPct(r.max_drawdown_pct)}</TableCell>
                      <TableCell className="text-right">{r.trades}</TableCell>
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
          <CardTitle>New configuration</CardTitle>
          <CardDescription>
            {baseId
              ? `Starting from v${data.configs.find((c) => c.id === baseId)?.version_no}.`
              : 'Starting from defaults.'}{' '}
            Capital, sizing, timing and risk limits are deployment settings. The strategy's own
            logic and model are part of its version and are not changed here.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4"
            onSubmit={(e) => {
              e.preventDefault()
              save.mutate()
            }}
          >
            <Field
              id="capital"
              label="Capital (Rs)"
              value={form.capital}
              onChange={set('capital')}
            />
            <Field
              id="allocation"
              label="Allocation (%)"
              value={form.allocationPct}
              onChange={set('allocationPct')}
            />
            <Field
              id="leverage"
              label="Leverage (x)"
              value={form.leverage}
              onChange={set('leverage')}
            />
            <div className="space-y-1.5">
              <Label>Product</Label>
              <Select value={form.product} onValueChange={(v) => set('product')(v)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="MIS">MIS (intraday)</SelectItem>
                  <SelectItem value="CNC">CNC (delivery)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <Field
              id="entry"
              label="Entry from (IST)"
              type="time"
              value={form.entry_time}
              onChange={set('entry_time')}
            />
            <Field
              id="exit"
              label="Exit at (IST)"
              type="time"
              value={form.exit_time}
              onChange={set('exit_time')}
            />
            <Field
              id="stop"
              label="Stop loss (%)"
              placeholder="Off"
              value={form.stopPct}
              onChange={set('stopPct')}
            />
            <Field
              id="target"
              label="Target (%)"
              placeholder="Off"
              value={form.targetPct}
              onChange={set('targetPct')}
            />
            <div className="space-y-1.5">
              <Label>Exchange</Label>
              <Select value={form.exchange} onValueChange={(v) => set('exchange')(v)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="NSE">NSE</SelectItem>
                  <SelectItem value="BSE">BSE</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5 sm:col-span-2 lg:col-span-3">
              <Label htmlFor="universe">Universe (symbols, comma separated)</Label>
              <Textarea
                id="universe"
                rows={2}
                value={form.universe}
                onChange={(e) => set('universe')(e.target.value)}
                placeholder="RELIANCE, INFY, TCS"
              />
            </div>
            <div className="space-y-1.5 sm:col-span-2 lg:col-span-3">
              <Label htmlFor="note">Note (optional)</Label>
              <Input id="note" value={form.note} onChange={(e) => set('note')(e.target.value)} />
            </div>
            <div className="flex items-end">
              <Button type="submit" disabled={save.isPending} className="w-full">
                Save as new version
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}

function Field(props: {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
  type?: string
  placeholder?: string
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={props.id}>{props.label}</Label>
      <Input
        id={props.id}
        type={props.type ?? 'text'}
        inputMode={props.type ? undefined : 'decimal'}
        placeholder={props.placeholder}
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
      />
    </div>
  )
}
