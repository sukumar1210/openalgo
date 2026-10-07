// pages/research/DeployToSandbox.tsx
// Deploy a scenario to the Sandbox on a schedule, the way Python strategies are
// scheduled: a start and a stop time in IST on chosen days. Sessions trade live
// market data with fills from OpenAlgo's sandbox and never reach a broker.

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { createDeployment, researchQueryKeys, type Scenario } from '@/api/research'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { showToast } from '@/utils/toast'

const DAYS: { value: string; label: string }[] = [
  { value: 'mon', label: 'Mon' },
  { value: 'tue', label: 'Tue' },
  { value: 'wed', label: 'Wed' },
  { value: 'thu', label: 'Thu' },
  { value: 'fri', label: 'Fri' },
  { value: 'sat', label: 'Sat' },
  { value: 'sun', label: 'Sun' },
]

export function DeployToSandbox({
  scenario,
  open,
  onOpenChange,
}: {
  scenario: Scenario
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [name, setName] = useState(scenario.name)
  const [start, setStart] = useState('09:10')
  const [stop, setStop] = useState('15:25')
  const [days, setDays] = useState<string[]>(['mon', 'tue', 'wed', 'thu', 'fri'])
  const [policy, setPolicy] = useState<'PAUSE' | 'FAIL_SAFE'>('PAUSE')

  const deploy = useMutation({
    mutationFn: () =>
      createDeployment({
        scenario_id: scenario.id,
        name,
        schedule_start: start,
        schedule_stop: stop,
        schedule_days: days,
        stale_policy: policy,
      }),
    onSuccess: (deployment) => {
      queryClient.invalidateQueries({ queryKey: researchQueryKeys.deployments() })
      onOpenChange(false)
      navigate(`/research/deployments/${deployment.id}`)
    },
    onError: (err: Error) => showToast.error(err.message),
  })

  const toggle = (day: string, on: boolean) =>
    setDays((all) => (on ? [...all, day] : all.filter((d) => d !== day)))

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Deploy to the Sandbox</DialogTitle>
          <DialogDescription>
            Each scheduled day this scenario trades live market data in its own session. Every order
            is filled by OpenAlgo's sandbox, with each strategy's own capital and ledger as in the
            backtest.
          </DialogDescription>
        </DialogHeader>

        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 rounded-md border p-3 text-sm">
          <dt className="text-muted-foreground">Environment</dt>
          <dd className="font-medium">Sandbox</dd>
          <dt className="text-muted-foreground">Broker orders</dt>
          <dd className="font-medium">Off. Orders go to the sandbox only.</dd>
          <dt className="text-muted-foreground">Market data</dt>
          <dd className="font-medium">Live, from your broker session</dd>
        </dl>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5 sm:col-span-2">
            <Label htmlFor="dep-name">Name</Label>
            <Input id="dep-name" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="dep-start">Start (IST)</Label>
            <Input
              id="dep-start"
              type="time"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="dep-stop">Stop (IST)</Label>
            <Input
              id="dep-stop"
              type="time"
              value={stop}
              onChange={(e) => setStop(e.target.value)}
            />
          </div>
          <fieldset className="space-y-1.5 sm:col-span-2">
            <legend className="text-sm font-medium">Days</legend>
            <div className="flex flex-wrap gap-3">
              {DAYS.map((day) => (
                <span key={day.value} className="flex items-center gap-1.5 text-sm">
                  <Checkbox
                    id={`dep-day-${day.value}`}
                    checked={days.includes(day.value)}
                    onCheckedChange={(on) => toggle(day.value, on === true)}
                  />
                  <Label htmlFor={`dep-day-${day.value}`}>{day.label}</Label>
                </span>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">
              Days the exchange is closed are skipped automatically.
            </p>
          </fieldset>
          <div className="space-y-1.5 sm:col-span-2">
            <Label>If market data stops arriving</Label>
            <Select value={policy} onValueChange={(v) => setPolicy(v as 'PAUSE' | 'FAIL_SAFE')}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="PAUSE">Pause new trades until it returns</SelectItem>
                <SelectItem value="FAIL_SAFE">Close positions and end the session</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={() => deploy.mutate()} disabled={deploy.isPending || days.length === 0}>
            Deploy to the Sandbox
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
