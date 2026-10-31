// pages/python-strategy/StrategySettingsCard.tsx
// A strategy's own settings, handed to each of its runs as environment
// variables. Two copies of one script can then run with a different exit time
// or stop, where the shared .env would give both the same. Names start with
// ALGO_, so a setting can never replace what OpenAlgo itself hands a run.
// Changed only while the strategy is stopped.

import { useState } from 'react'
import { pythonStrategyApi } from '@/api/python-strategy'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import type { PythonStrategy } from '@/types/python-strategy'
import { showToast } from '@/utils/toast'

type Row = { name: string; value: string }

const NAME = /^ALGO_[A-Z0-9_]{1,60}$/

export function StrategySettingsCard({
  strategy,
  onChanged,
}: {
  strategy: PythonStrategy
  onChanged: (env: Record<string, string>) => void
}) {
  const [rows, setRows] = useState<Row[]>(
    Object.entries(strategy.env ?? {}).map(([name, value]) => ({ name, value }))
  )
  const [saving, setSaving] = useState(false)
  const running = strategy.status === 'running'
  const named = rows.filter((r) => r.name)
  const invalid = named.find((r) => !NAME.test(r.name))
  const duplicate = new Set(named.map((r) => r.name)).size !== named.length

  const edit = (i: number, change: Partial<Row>) =>
    setRows(rows.map((r, j) => (j === i ? { ...r, ...change } : r)))

  const save = async () => {
    try {
      setSaving(true)
      const env = await pythonStrategyApi.setSettings(
        strategy.id,
        Object.fromEntries(named.map((r) => [r.name, r.value]))
      )
      onChanged(env)
      showToast.success('Settings saved. They apply from the next run.', 'pythonStrategy')
    } catch (e) {
      const message = (e as { response?: { data?: { message?: string } } }).response?.data?.message
      showToast.error(message || 'Could not save the settings. Try again.', 'pythonStrategy')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Settings</CardTitle>
        <CardDescription>
          {running
            ? 'Stop the strategy to change these.'
            : 'Handed to each run as environment variables. Names start with ALGO_.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {rows.map((row, i) => (
          <div key={i} className="flex gap-2">
            <Input
              aria-label={`Setting ${i + 1} name`}
              placeholder="ALGO_PAPER_EXIT_TIME"
              value={row.name}
              disabled={running || saving}
              onChange={(e) => edit(i, { name: e.target.value.toUpperCase() })}
            />
            <Input
              aria-label={`Setting ${i + 1} value`}
              placeholder="15:10"
              value={row.value}
              disabled={running || saving}
              onChange={(e) => edit(i, { value: e.target.value })}
            />
            <Button
              variant="ghost"
              disabled={running || saving}
              onClick={() => setRows(rows.filter((_, j) => j !== i))}
            >
              Remove
            </Button>
          </div>
        ))}
        {invalid && (
          <p className="text-sm text-destructive">
            {invalid.name} is not a setting a strategy can be given: names start with ALGO_ and use
            capital letters, digits and underscores.
          </p>
        )}
        {duplicate && <p className="text-sm text-destructive">Each name can be used once.</p>}
        <div className="flex gap-2">
          <Button
            variant="outline"
            disabled={running || saving}
            onClick={() => setRows([...rows, { name: '', value: '' }])}
          >
            Add setting
          </Button>
          <Button disabled={running || saving || !!invalid || duplicate} onClick={save}>
            {saving ? 'Saving...' : 'Save'}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
