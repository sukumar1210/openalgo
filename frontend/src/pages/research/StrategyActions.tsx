// pages/research/StrategyActions.tsx
// The registry row's actions (PRD section 32): backtest the strategy, deploy it,
// and pause or stop everything it is trading in.

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import {
  backtestStrategy,
  controlStrategy,
  researchQueryKeys,
  type Scenario,
  type StrategyListItem,
  strategyScenario,
} from '@/api/research'
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { showToast } from '@/utils/toast'
import { DeployToSandbox } from './DeployToSandbox'

function isoDay(offsetDays: number): string {
  const day = new Date(Date.now() + offsetDays * 86_400_000)
  return day.toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' })
}

export function StrategyActions({ strategy }: { strategy: StrategyListItem }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [backtesting, setBacktesting] = useState(false)
  const [start, setStart] = useState(isoDay(-90))
  const [end, setEnd] = useState(isoDay(-1))
  const [deploying, setDeploying] = useState<Scenario | null>(null)
  const [confirm, setConfirm] = useState<'pause' | 'stop' | null>(null)
  const ready = strategy.status !== 'not_installed' && strategy.latest_config !== null
  const notReady = ready
    ? undefined
    : strategy.status === 'not_installed'
      ? 'Its package is not installed on this server'
      : 'Open the strategy and save a configuration first'

  const backtest = useMutation({
    mutationFn: () => backtestStrategy(strategy.id, { start, end }),
    onSuccess: (run) => {
      if (!run.created && run.status === 'COMPLETED') {
        showToast.info(`Run #${run.id} already has the result for exactly these inputs`)
      }
      navigate(`/research/runs/${run.id}`)
    },
    onError: (err: Error) => showToast.error(err.message),
  })
  const prepare = useMutation({
    mutationFn: () => strategyScenario(strategy.id),
    onSuccess: setDeploying,
    onError: (err: Error) => showToast.error(err.message),
  })
  const control = useMutation({
    mutationFn: (action: 'pause' | 'stop') => controlStrategy(strategy.id, action),
    onSuccess: (items, action) => {
      queryClient.invalidateQueries({ queryKey: researchQueryKeys.all })
      const done = action === 'pause' ? 'paused' : 'stopped'
      showToast.success(
        items.length ? `${items.length} ${done}` : `Nothing to ${action}: it is not trading now`
      )
    },
    onError: (err: Error) => showToast.error(err.message),
  })

  return (
    <div className="flex flex-wrap justify-end gap-1">
      {/* A disabled button takes no pointer events, so the reason sits on a wrapper. */}
      <span title={notReady}>
        <Button size="sm" variant="outline" disabled={!ready} onClick={() => setBacktesting(true)}>
          Backtest
        </Button>
      </span>
      <span title={notReady}>
        <Button
          size="sm"
          variant="outline"
          disabled={!ready || prepare.isPending}
          onClick={() => prepare.mutate()}
        >
          Deploy
        </Button>
      </span>
      <Button
        size="sm"
        variant="ghost"
        disabled={strategy.status !== 'trading'}
        onClick={() => setConfirm('pause')}
      >
        Pause
      </Button>
      <Button
        size="sm"
        variant="ghost"
        disabled={strategy.active_deployments === 0 && strategy.status !== 'trading'}
        onClick={() => setConfirm('stop')}
      >
        Stop
      </Button>

      <Dialog open={backtesting} onOpenChange={setBacktesting}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Backtest {strategy.key}</DialogTitle>
            <DialogDescription>
              Its newest version with configuration v{strategy.latest_config?.version_no}, alone, on
              the bars stored in Historify for these dates.
            </DialogDescription>
          </DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor={`bt-start-${strategy.id}`}>From</Label>
              <Input
                id={`bt-start-${strategy.id}`}
                type="date"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`bt-end-${strategy.id}`}>To</Label>
              <Input
                id={`bt-end-${strategy.id}`}
                type="date"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </div>
          </div>
          <DialogFooter>
            <Button disabled={backtest.isPending} onClick={() => backtest.mutate()}>
              Run backtest
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {deploying && (
        <DeployToSandbox
          scenario={deploying}
          open
          onOpenChange={(open) => !open && setDeploying(null)}
        />
      )}

      <AlertDialog open={confirm !== null} onOpenChange={(open) => !open && setConfirm(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirm === 'pause' ? `Pause ${strategy.key}?` : `Stop ${strategy.key}?`}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirm === 'pause'
                ? 'Every running session that includes it stops taking new trades. Positions stay open and exits still run. Other strategies in the same session pause too.'
                : 'Every session that includes it closes its positions and ends, and its deployments stop scheduling new sessions. Other strategies in the same session stop too.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => confirm && control.mutate(confirm)}>
              {confirm === 'pause' ? 'Pause' : 'Close positions and stop'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
