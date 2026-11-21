// pages/python-strategy/StrategyAccountCard.tsx
// A Sandbox account of the strategy's own. Without one, a strategy in Sandbox
// mode trades the main Sandbox, where two strategies holding the same stock
// net into one position. With one, its orders, positions, square-off and the
// Sandbox's daily loss limit (a share of this account's own capital) are its
// own, and each run is handed what the account is worth. Opening one sets the
// strategy to Sandbox mode. Everything here changes only while it is stopped.

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
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { makeFormatCurrency } from '@/lib/utils'
import type { PythonStrategy, StrategyAccount } from '@/types/python-strategy'
import { showToast } from '@/utils/toast'

const money = makeFormatCurrency()

function refusal(e: unknown, fallback: string): string {
  return (e as { response?: { data?: { message?: string } } }).response?.data?.message || fallback
}

export function StrategyAccountCard({
  strategy,
  onChanged,
}: {
  strategy: PythonStrategy
  onChanged: (account: StrategyAccount | null) => void
}) {
  const [label, setLabel] = useState('')
  const [capital, setCapital] = useState('500000')
  const [amount, setAmount] = useState('')
  const [busy, setBusy] = useState(false)
  const running = strategy.status === 'running'
  const account = strategy.account?.status === 'active' ? strategy.account : null

  const act = async (work: () => Promise<void>) => {
    try {
      setBusy(true)
      await work()
    } finally {
      setBusy(false)
    }
  }

  const open = () =>
    act(async () => {
      try {
        const opened = await pythonStrategyApi.bindAccount(strategy.id, label, Number(capital))
        onChanged(opened)
        showToast.success(`Opened ${opened.label} with ${money(Number(capital))}`, 'pythonStrategy')
      } catch (e) {
        showToast.error(refusal(e, 'Could not open the account. Try again.'), 'pythonStrategy')
      }
    })

  const fund = (sign: 1 | -1) =>
    act(async () => {
      try {
        const updated = await pythonStrategyApi.fundAccount(strategy.id, sign * Number(amount))
        onChanged(updated)
        setAmount('')
      } catch (e) {
        showToast.error(refusal(e, 'Could not move the money. Try again.'), 'pythonStrategy')
      }
    })

  const close = () =>
    act(async () => {
      try {
        await pythonStrategyApi.closeAccount(strategy.id)
        onChanged(null)
        showToast.success('Account closed. It trades the main Sandbox again.', 'pythonStrategy')
      } catch (e) {
        showToast.error(refusal(e, 'Could not close the account. Try again.'), 'pythonStrategy')
      }
    })

  // Bound, but the account list cannot say to what (accounts turned off, or no owner
  // recorded). Nothing is offered: opening another or closing would lose track of it.
  if (strategy.account?.status === 'unavailable') {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Sandbox account</CardTitle>
          <CardDescription>{strategy.account.message}</CardDescription>
        </CardHeader>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Sandbox account</CardTitle>
        <CardDescription>
          {running
            ? 'Stop the strategy to change this.'
            : account
              ? 'It trades only this account: its own money, positions and daily loss limit.'
              : 'It trades the main Sandbox. Give it an account of its own so its positions never net with another strategy’s. It then runs in Sandbox mode.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {account ? (
          <>
            <p className="text-sm">
              <span className="font-medium">{account.label}</span>
              {account.equity != null && <> is worth {money(account.equity)}</>}
              {account.starting_capital != null && (
                <span className="text-muted-foreground">
                  {' '}
                  (opened with {money(account.starting_capital)})
                </span>
              )}
            </p>
            <div className="flex items-end gap-2">
              <div className="space-y-2">
                <Label htmlFor="account-amount">Amount</Label>
                <Input
                  id="account-amount"
                  type="number"
                  min={1}
                  step={1000}
                  value={amount}
                  disabled={running || busy}
                  onChange={(e) => setAmount(e.target.value)}
                />
              </div>
              <Button
                variant="outline"
                disabled={running || busy || !(Number(amount) > 0)}
                onClick={() => fund(1)}
              >
                Add
              </Button>
              <Button
                variant="outline"
                disabled={running || busy || !(Number(amount) > 0)}
                onClick={() => fund(-1)}
              >
                Take out
              </Button>
            </div>
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button variant="destructive" disabled={running || busy}>
                  Close account
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Close {account.label}?</AlertDialogTitle>
                  <AlertDialogDescription>
                    Only an account holding no position, resting order or GTT can be closed. The
                    strategy stays in Sandbox mode and trades the main Sandbox from its next run.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Keep it</AlertDialogCancel>
                  <AlertDialogAction onClick={close}>Close account</AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </>
        ) : (
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              open()
            }}
          >
            <div className="space-y-2">
              <Label htmlFor="account-label">Name</Label>
              <Input
                id="account-label"
                placeholder="alpha_a_1510"
                value={label}
                disabled={running || busy}
                onChange={(e) => setLabel(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="account-capital">Capital</Label>
              <Input
                id="account-capital"
                type="number"
                min={10000}
                step={1000}
                value={capital}
                disabled={running || busy}
                onChange={(e) => setCapital(e.target.value)}
              />
            </div>
            <Button type="submit" disabled={running || busy || !label || !(Number(capital) > 0)}>
              {busy ? 'Opening...' : 'Open account'}
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  )
}
