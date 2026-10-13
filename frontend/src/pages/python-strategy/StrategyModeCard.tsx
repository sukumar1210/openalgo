// pages/python-strategy/StrategyModeCard.tsx
// Where a Python strategy's orders go. Sandbox keeps every order in the Sandbox
// whatever Analyzer mode says; Live follows Analyzer mode, which stays the
// platform's master switch. The run is handed its key when it starts, so the
// mode only changes while the strategy is stopped.

import { useState } from 'react'
import { pythonStrategyApi } from '@/api/python-strategy'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import type { PythonStrategy, StrategyMode } from '@/types/python-strategy'
import { showToast } from '@/utils/toast'

const MODES: { value: StrategyMode; label: string; explains: string }[] = [
  {
    value: 'sandbox',
    label: 'Sandbox',
    explains: 'Every order goes to the Sandbox, whether Analyzer mode is on or off.',
  },
  {
    value: 'live',
    label: 'Live',
    explains:
      'Orders go to your broker. While Analyzer mode is on, the whole platform stays in the Sandbox, this strategy included.',
  },
]

const CONFIRM: Record<StrategyMode, { title: string; body: string; action: string }> = {
  live: {
    title: 'Send this strategy’s orders to your broker?',
    body: 'From its next start, whenever Analyzer mode is off, its orders reach your broker with real money.',
    action: 'Switch to Live',
  },
  sandbox: {
    title: 'Keep this strategy in the Sandbox?',
    body: 'From its next start, its orders go to the Sandbox only. If it holds a position at your broker, close it first: from the Sandbox, its orders can no longer reach that position.',
    action: 'Switch to Sandbox',
  },
}

export function StrategyModeCard({
  strategy,
  onChanged,
}: {
  strategy: PythonStrategy
  onChanged: (mode: StrategyMode) => void
}) {
  const [asking, setAsking] = useState<StrategyMode | null>(null)
  const [saving, setSaving] = useState(false)
  const running = strategy.status === 'running'
  const current = strategy.mode ?? 'live'

  const confirm = async () => {
    if (!asking) return
    try {
      setSaving(true)
      const result = await pythonStrategyApi.setMode(strategy.id, asking)
      onChanged(asking)
      showToast.success(result.message || 'Mode saved', 'pythonStrategy')
    } catch (e) {
      const message = (e as { response?: { data?: { message?: string } } }).response?.data?.message
      showToast.error(message || 'Could not change the mode. Try again.', 'pythonStrategy')
    } finally {
      setSaving(false)
      setAsking(null)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Where its orders go</CardTitle>
        <CardDescription>
          {running
            ? 'Stop the strategy to change this. It applies from the next start.'
            : 'Applies from the next start.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex gap-2" role="radiogroup" aria-label="Mode">
          {MODES.map((mode) => (
            <Button
              key={mode.value}
              type="button"
              role="radio"
              aria-checked={current === mode.value}
              variant={current === mode.value ? 'default' : 'outline'}
              disabled={running || saving}
              onClick={() => current !== mode.value && setAsking(mode.value)}
            >
              {mode.label}
            </Button>
          ))}
        </div>
        <p className="text-sm text-muted-foreground">
          {MODES.find((m) => m.value === current)?.explains}
        </p>
      </CardContent>
      <AlertDialog open={asking !== null} onOpenChange={(open) => !open && setAsking(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{asking && CONFIRM[asking].title}</AlertDialogTitle>
            <AlertDialogDescription>{asking && CONFIRM[asking].body}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirm} disabled={saving}>
              {asking && CONFIRM[asking].action}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}
