// pages/python-strategy/StrategySettingsCard.tsx
// A strategy's own settings, handed to each of its runs as environment
// variables. Two copies of one script can then run with a different exit time
// or stop, where the shared .env would give both the same. Only a strategy
// trading its own Sandbox account can be given settings, and the card offers
// exactly the names the server lists (strategy.setting_names): it keeps no list
// of its own, so the two can never disagree. A strategy left with settings and
// no account is refused at start, so the card then offers only to clear them.
// Changed only while the strategy is stopped.

import { useState } from 'react'
import { pythonStrategyApi } from '@/api/python-strategy'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { PythonStrategy } from '@/types/python-strategy'
import { showToast } from '@/utils/toast'

export function StrategySettingsCard({
  strategy,
  onChanged,
}: {
  strategy: PythonStrategy
  onChanged: (env: Record<string, string>) => void
}) {
  const names = strategy.setting_names ?? []
  const stored = strategy.env ?? {}
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(names.map((name) => [name, stored[name] ?? '']))
  )
  const [saving, setSaving] = useState(false)
  const running = strategy.status === 'running'
  const ownAccount = strategy.account?.status === 'active'
  // Saved before the list was narrowed, or by hand: shown, and dropped on save.
  const notAllowed = Object.keys(stored).filter((name) => !names.includes(name))

  const save = async (env: Record<string, string>) => {
    try {
      setSaving(true)
      const saved = await pythonStrategyApi.setSettings(strategy.id, env)
      onChanged(saved)
      showToast.success('Settings saved. They apply from the next run.', 'pythonStrategy')
    } catch (e) {
      const message = (e as { response?: { data?: { message?: string } } }).response?.data?.message
      showToast.error(message || 'Could not save the settings. Try again.', 'pythonStrategy')
    } finally {
      setSaving(false)
    }
  }

  if (!ownAccount) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Settings</CardTitle>
          <CardDescription>
            Settings apply only to a strategy trading its own Sandbox account, so this strategy will
            not start while it has some. Clear them, or open an account for it.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button variant="outline" disabled={running || saving} onClick={() => save({})}>
            Clear settings
          </Button>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Settings</CardTitle>
        <CardDescription>
          {running
            ? 'Stop the strategy to change these.'
            : 'Handed to each run as environment variables. Leave one empty to use the shared setting.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {names.map((name) => (
          <div key={name} className="space-y-1">
            <Label htmlFor={`setting-${name}`}>{name}</Label>
            <Input
              id={`setting-${name}`}
              value={values[name] ?? ''}
              disabled={running || saving}
              onChange={(e) => setValues({ ...values, [name]: e.target.value })}
            />
          </div>
        ))}
        {notAllowed.length > 0 && (
          <p className="text-sm text-destructive">
            {notAllowed.join(', ')} {notAllowed.length === 1 ? 'is' : 'are'} not a setting a
            strategy can be given, so this strategy will not start. Save to remove{' '}
            {notAllowed.length === 1 ? 'it' : 'them'}.
          </p>
        )}
        <Button
          disabled={running || saving}
          onClick={() =>
            save(Object.fromEntries(Object.entries(values).filter(([, value]) => value !== '')))
          }
        >
          {saving ? 'Saving...' : 'Save'}
        </Button>
      </CardContent>
    </Card>
  )
}
