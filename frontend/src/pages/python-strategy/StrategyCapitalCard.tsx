// pages/python-strategy/StrategyCapitalCard.tsx
// A strategy's own capital. It starts with the amount set here, and every day
// it is handed that plus its own net P&L (after charges) of every earlier day,
// as OPENALGO_STRATEGY_CAPITAL: a day's result is the strategy's own to use
// from the next day, and one strategy's loss never reaches another's capital.
// The run is handed its capital when it starts, so it changes only while the
// strategy is stopped.

import { useState } from 'react'
import { pythonStrategyApi } from '@/api/python-strategy'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { makeFormatCurrency } from '@/lib/utils'
import type { PythonStrategy } from '@/types/python-strategy'
import { showToast } from '@/utils/toast'

const money = makeFormatCurrency()

export function StrategyCapitalCard({
  strategy,
  onChanged,
}: {
  strategy: PythonStrategy
  onChanged: (capital: number) => void
}) {
  const [draft, setDraft] = useState(String(strategy.capital))
  const [saving, setSaving] = useState(false)
  const running = strategy.status === 'running'
  const amount = Number(draft)

  const save = async () => {
    try {
      setSaving(true)
      const capital = await pythonStrategyApi.setCapital(strategy.id, amount)
      onChanged(capital)
      showToast.success(`Starts with ${money(capital)}`, 'pythonStrategy')
    } catch (e) {
      const message = (e as { response?: { data?: { message?: string } } }).response?.data?.message
      showToast.error(message || 'Could not change the capital. Try again.', 'pythonStrategy')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Capital</CardTitle>
        <CardDescription>
          {running
            ? 'Stop the strategy to change this. It applies from the next start.'
            : 'Each day it is handed this plus its own net P&L, after charges, of every earlier day.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <form
          className="flex items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            save()
          }}
        >
          <div className="space-y-2">
            <Label htmlFor="strategy-capital">Starting capital</Label>
            <Input
              id="strategy-capital"
              type="number"
              min={10000}
              step={1000}
              value={draft}
              disabled={running || saving}
              onChange={(e) => setDraft(e.target.value)}
            />
          </div>
          <Button
            type="submit"
            disabled={running || saving || !(amount > 0) || amount === strategy.capital}
          >
            {saving ? 'Saving...' : 'Save'}
          </Button>
        </form>
        <p className="text-sm text-muted-foreground">
          {strategy.capital_today == null
            ? 'Its capital for today could not be worked out right now.'
            : `A run starting today gets ${money(strategy.capital_today)}.`}
        </p>
      </CardContent>
    </Card>
  )
}
