// pages/runs/PnlDashboard.tsx
// One page for checking P&L: every Run, one strategy within a Run, the main
// Sandbox account and the strategies trading outside Runs, over any window,
// each on its own or added up. The report itself is PnlReport.

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { listRuns, type PnlQuery, runKeys } from '@/api/runs'
import { getDashboard, strategyDashboardKeys } from '@/api/strategy-dashboard'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'
import { PnlReport, type View } from './PnlReport'
import { RunsNav } from './shared'

export type Preset = 'today' | 'week' | 'month' | 'year' | 'all' | 'custom'

const PRESETS: [Preset, string][] = [
  ['today', 'Today'],
  ['week', 'This week'],
  ['month', 'This month'],
  ['year', 'This year'],
  ['all', 'All'],
  ['custom', 'Custom'],
]

const isoOf = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`

/** The first and last day a preset covers, ending today; none for All. */
export function windowOf(preset: Preset, today: Date = new Date()): { from?: string; to?: string } {
  const end = isoOf(today)
  if (preset === 'today') return { from: end, to: end }
  if (preset === 'week') {
    const monday = new Date(today)
    monday.setDate(today.getDate() - ((today.getDay() + 6) % 7))
    return { from: isoOf(monday), to: end }
  }
  if (preset === 'month') return { from: `${end.slice(0, 7)}-01`, to: end }
  if (preset === 'year') return { from: `${end.slice(0, 4)}-01-01`, to: end }
  return {}
}

export default function PnlDashboard() {
  const [params] = useSearchParams()
  const runs = useQuery({ queryKey: runKeys.list(), queryFn: listRuns })
  const outside = useQuery({
    queryKey: strategyDashboardKeys.data('', '', []),
    queryFn: () => getDashboard('', '', []),
  })
  const preset0 = params.get('runs')?.split(',').map(Number).filter(Boolean) ?? null
  const [picked, setPicked] = useState<number[] | null>(preset0)
  const [main, setMain] = useState(false)
  const [others, setOthers] = useState(false)
  const [seat, setSeat] = useState('')
  const [preset, setPreset] = useState<Preset>('month')
  const [custom, setCustom] = useState({ from: '', to: '' })
  const [view, setView] = useState<View>('individual')

  const traded = (runs.data ?? []).filter((r) => r.status !== 'draft')
  // Every Run that has traded is shown until the trader chooses.
  const chosen = picked ?? traded.map((r) => r.id)
  const single = chosen.length === 1 ? traded.find((r) => r.id === chosen[0]) : undefined
  const strategyKeys = (outside.data?.strategies ?? []).map((s) => s.key)
  const span = preset === 'custom' ? custom : windowOf(preset)

  // The query key is this object as JSON, so a new object each render asks nothing new.
  const query: PnlQuery = {
    runs: single && seat ? [] : chosen,
    members: single && seat ? [[single.id, seat]] : [],
    main,
    strategies: others ? strategyKeys : [],
    from: span.from || undefined,
    to: span.to || undefined,
  }

  const toggle = (id: number) => {
    setSeat('')
    setPicked((ids) => {
      const now = ids ?? traded.map((r) => r.id)
      return now.includes(id) ? now.filter((i) => i !== id) : [...now, id]
    })
  }

  return (
    <div className="container mx-auto space-y-6 px-4 py-6">
      <RunsNav />
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">P&L</h1>
          <p className="text-muted-foreground">
            Every Run, the main Sandbox account and the strategies outside Runs. Every figure is
            after charges and agrees with the accounts.
          </p>
        </div>
        <Button asChild variant="outline">
          <Link to="/strategy-dashboard">Each strategy's ledger</Link>
        </Button>
      </div>

      <Card>
        <CardContent className="space-y-4 pt-6">
          <fieldset>
            <legend className="mb-2 text-sm font-medium">Show</legend>
            <div className="flex flex-wrap gap-2">
              {traded.map((run) => (
                <Chip
                  key={run.id}
                  label={run.name}
                  on={chosen.includes(run.id)}
                  onToggle={() => toggle(run.id)}
                />
              ))}
              <Chip label="Main Sandbox account" on={main} onToggle={() => setMain((v) => !v)} />
              <Chip
                label="Strategies outside Runs"
                on={others}
                disabled={!strategyKeys.length}
                onToggle={() => setOthers((v) => !v)}
              />
            </div>
            {!runs.isLoading && traded.length === 0 && (
              <p className="mt-2 text-sm text-muted-foreground">
                No Run has traded yet; the main account and the strategies outside Runs are here
                already.
              </p>
            )}
          </fieldset>
          <div className="flex flex-wrap items-end gap-4">
            <div className="flex flex-wrap gap-1" role="radiogroup" aria-label="Window">
              {PRESETS.map(([key, label]) => (
                <Button
                  key={key}
                  role="radio"
                  aria-checked={preset === key}
                  variant={preset === key ? 'default' : 'outline'}
                  size="sm"
                  onClick={() => setPreset(key)}
                >
                  {label}
                </Button>
              ))}
            </div>
            {preset === 'custom' && (
              <div className="flex gap-2">
                <div className="space-y-1">
                  <Label htmlFor="pnl-from">From</Label>
                  <Input
                    id="pnl-from"
                    type="date"
                    value={custom.from}
                    onChange={(e) => setCustom((c) => ({ ...c, from: e.target.value }))}
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="pnl-to">To</Label>
                  <Input
                    id="pnl-to"
                    type="date"
                    value={custom.to}
                    onChange={(e) => setCustom((c) => ({ ...c, to: e.target.value }))}
                  />
                </div>
              </div>
            )}
            <div className="flex gap-1" role="radiogroup" aria-label="Lines">
              {(['individual', 'combined'] as const).map((v) => (
                <Button
                  key={v}
                  role="radio"
                  aria-checked={view === v}
                  variant={view === v ? 'default' : 'outline'}
                  size="sm"
                  onClick={() => setView(v)}
                >
                  {v === 'individual' ? 'Each on its own' : 'Combined'}
                </Button>
              ))}
            </div>
            {single && (
              <label className="flex items-center gap-2 text-sm">
                Strategy within the Run
                <select
                  value={seat}
                  onChange={(e) => setSeat(e.target.value)}
                  className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
                >
                  <option value="">The whole Run</option>
                  {single.version.members.map((m) => (
                    <option key={m.seat} value={m.seat}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
        </CardContent>
      </Card>

      <PnlReport query={query} view={view} />
    </div>
  )
}

function Chip({
  label,
  on,
  onToggle,
  disabled,
}: {
  label: string
  on: boolean
  onToggle: () => void
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      disabled={disabled}
      onClick={onToggle}
      className={cn(
        'rounded-full border px-3 py-1.5 text-sm',
        on ? 'border-primary bg-primary/10 font-medium' : 'border-border text-muted-foreground',
        disabled && 'cursor-not-allowed opacity-50'
      )}
    >
      {label}
    </button>
  )
}
