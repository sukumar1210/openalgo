// pages/research/DeployToSandbox.tsx
// Deploy a scenario on a schedule, the way Python strategies are scheduled: a
// start and a stop time in IST on chosen days. Each day's session trades live
// market data in the environment chosen here, fixed for the deployment: the
// Sandbox (OpenAlgo's sandbox fills it), Shadow (filled inside the session,
// nothing sent anywhere) or Live (real orders to the broker, behind its gate).

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import {
  createDeployment,
  ENVIRONMENT_LABEL,
  getLiveOverview,
  researchQueryKeys,
  type Scenario,
  type SessionEnvironment,
} from '@/api/research'
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
  const [environment, setEnvironment] = useState<SessionEnvironment>('SANDBOX')
  const [capitalLimit, setCapitalLimit] = useState('')
  const [confirm, setConfirm] = useState('')
  const live = useQuery({
    queryKey: researchQueryKeys.live(),
    queryFn: getLiveOverview,
    enabled: open,
    retry: false,
  })
  const liveAllowed = live.data?.enabled === true && !live.data.kill_switch.engaged
  const isLive = environment === 'LIVE'

  const deploy = useMutation({
    mutationFn: () =>
      createDeployment({
        scenario_id: scenario.id,
        name,
        schedule_start: start,
        schedule_stop: stop,
        schedule_days: days,
        stale_policy: policy,
        environment,
        ...(isLive ? { capital_limit: Number(capitalLimit), confirm } : {}),
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
          <DialogTitle>Deploy {scenario.name}</DialogTitle>
          <DialogDescription>
            Each scheduled day this scenario trades live market data in its own session, with each
            strategy's own capital and ledger as in the backtest.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-1.5">
          <Label>Where orders go</Label>
          <Select
            value={environment}
            onValueChange={(v) => setEnvironment(v as SessionEnvironment)}
          >
            <SelectTrigger aria-label="Where orders go">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="SANDBOX">Sandbox: OpenAlgo's sandbox fills each order</SelectItem>
              <SelectItem value="SHADOW">
                Shadow: filled inside the session, nothing sent
              </SelectItem>
              <SelectItem value="LIVE" disabled={!liveAllowed}>
                Live: real orders to your broker
              </SelectItem>
            </SelectContent>
          </Select>
          {!liveAllowed && live.data && (
            <p className="text-xs text-muted-foreground">
              {live.data.kill_switch.engaged
                ? 'Live is unavailable while the kill switch is on.'
                : "Live is off on this server. Set RESEARCH_LIVE_ENABLED = 'True' in .env and restart OpenAlgo to allow it."}
            </p>
          )}
        </div>

        <dl
          className={`grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 rounded-md border p-3 text-sm ${
            isLive ? 'border-destructive bg-destructive/5' : ''
          }`}
        >
          <dt className="text-muted-foreground">Environment</dt>
          <dd className="font-medium">{ENVIRONMENT_LABEL[environment]}</dd>
          <dt className="text-muted-foreground">Broker orders</dt>
          <dd className={`font-medium ${isLive ? 'text-destructive' : ''}`}>
            {environment === 'SANDBOX'
              ? 'Off. Orders go to the sandbox only.'
              : environment === 'SHADOW'
                ? 'Off. Nothing is sent anywhere: each order is filled at the latest price inside the session.'
                : 'On. Real orders go to your broker, within the limit below.'}
          </dd>
          <dt className="text-muted-foreground">Market data</dt>
          <dd className="font-medium">Live, from your broker session</dd>
        </dl>

        {isLive && live.data && (
          <div className="space-y-3 rounded-md border border-destructive p-3 text-sm">
            <p>
              Live needs every strategy version allowed to trade live on its strategy page, and this
              scenario run in the Sandbox for at least one full day. A position-reducing order is
              always sent; anything that adds to a position must stay within the limit below.
            </p>
            <div className="space-y-1.5">
              <Label htmlFor="dep-limit">Most this deployment may hold (Rs)</Label>
              <Input
                id="dep-limit"
                inputMode="decimal"
                value={capitalLimit}
                onChange={(e) => setCapitalLimit(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="dep-confirm">Type the deployment's name to confirm: {name}</Label>
              <Input
                id="dep-confirm"
                autoComplete="off"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </div>
          </div>
        )}

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
          <Button
            variant={isLive ? 'destructive' : 'default'}
            onClick={() => deploy.mutate()}
            disabled={
              deploy.isPending ||
              days.length === 0 ||
              (isLive && (confirm.trim() !== name.trim() || !(Number(capitalLimit) > 0)))
            }
          >
            {environment === 'SANDBOX'
              ? 'Deploy to the Sandbox'
              : environment === 'SHADOW'
                ? 'Deploy in Shadow'
                : 'Deploy live'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
