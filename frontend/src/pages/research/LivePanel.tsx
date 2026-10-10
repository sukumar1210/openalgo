// pages/research/LivePanel.tsx
// Live, from the Research page: whether this server allows it and how much
// capital live deployments may hold, the kill switch, today's live sessions,
// and a check of what the platform expects against the broker's own record.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import {
  formatRupees,
  getLiveOverview,
  getLiveReconciliation,
  type LiveReconciliation,
  researchQueryKeys,
  type SandboxStatus,
  SESSION_STATUS_LABEL,
  setKillSwitch,
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
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { showToast } from '@/utils/toast'

export function LivePanel() {
  const queryClient = useQueryClient()
  const [engaging, setEngaging] = useState(false)
  const [reason, setReason] = useState('')
  const [check, setCheck] = useState<LiveReconciliation | null>(null)
  const { data } = useQuery({
    queryKey: researchQueryKeys.live(),
    queryFn: getLiveOverview,
    refetchInterval: 10_000,
  })
  const kill = useMutation({
    mutationFn: (engaged: boolean) => setKillSwitch(engaged, reason),
    onSuccess: (state) => {
      queryClient.invalidateQueries({ queryKey: researchQueryKeys.all })
      showToast.success(
        state.engaged
          ? 'Kill switch on: every live session is closing its positions'
          : 'Kill switch off: live sessions may open again'
      )
      setReason('')
    },
    onError: (err: Error) => showToast.error(err.message),
  })
  const reconcile = useMutation({
    mutationFn: getLiveReconciliation,
    onSuccess: setCheck,
    onError: (err: Error) => showToast.error(err.message),
  })

  if (!data) return null
  const engaged = data.kill_switch.engaged

  return (
    <Card className={engaged ? 'border-destructive' : undefined}>
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <div>
          <CardTitle className="flex items-center gap-2">
            Live{' '}
            <Badge variant={engaged ? 'destructive' : 'outline'}>
              {!data.enabled ? 'Off on this server' : engaged ? 'Kill switch on' : 'Allowed'}
            </Badge>
          </CardTitle>
          <CardDescription>
            {data.enabled
              ? `Active live deployments may hold up to ${formatRupees(data.allotted)} together.`
              : "Live trading from Research is off. To allow it, set RESEARCH_LIVE_ENABLED = 'True' in .env, then restart OpenAlgo."}
            {engaged && data.kill_switch.reason ? ` Reason: ${data.kill_switch.reason}` : ''}
          </CardDescription>
        </div>
        <div className="flex gap-2">
          {engaged ? (
            <Button variant="outline" disabled={kill.isPending} onClick={() => kill.mutate(false)}>
              Release kill switch
            </Button>
          ) : (
            <Button variant="destructive" onClick={() => setEngaging(true)}>
              Kill switch
            </Button>
          )}
          <Button
            variant="outline"
            disabled={reconcile.isPending}
            onClick={() => reconcile.mutate()}
          >
            Check against the broker
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {data.sessions.length === 0 ? (
          <p className="text-sm text-muted-foreground">No live session today.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {data.sessions.map((s) => (
              <li key={s.id}>
                <Link to={`/research/sessions/${s.id}`} className="hover:underline">
                  Session #{s.id}
                </Link>{' '}
                {SESSION_STATUS_LABEL[s.status as SandboxStatus] ?? s.status}
                {s.metrics ? `, ${formatRupees(s.metrics.net_pnl)}` : ''}
              </li>
            ))}
          </ul>
        )}

        {check && (
          <div className="space-y-2">
            <p className="text-sm text-muted-foreground">
              Checked at {check.checked_at.replace('T', ' ')}.{' '}
              {check.error ??
                'Other trading on the same account also shows in the broker column, so a difference is something to look at, not proof of an error.'}
            </p>
            {check.positions.length > 0 && (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Symbol</TableHead>
                    <TableHead className="text-right">Expected (Research)</TableHead>
                    <TableHead className="text-right">At the broker (intraday)</TableHead>
                    <TableHead className="text-right">Difference</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {check.positions.map((p) => (
                    <TableRow key={p.symbol} className={p.difference ? 'bg-destructive/5' : ''}>
                      <TableCell>{p.symbol}</TableCell>
                      <TableCell className="text-right">{p.platform}</TableCell>
                      <TableCell className="text-right">{p.broker ?? '-'}</TableCell>
                      <TableCell className="text-right">{p.difference ?? '-'}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            {check.unsettled_orders.length > 0 && (
              <ul className="list-disc pl-5 text-sm">
                {check.unsettled_orders.map((o) => (
                  <li key={o.platform_order_id}>
                    {o.symbol} {o.quantity > 0 ? 'buy' : 'sell'} {Math.abs(o.quantity)}:{' '}
                    {o.status === 'UNKNOWN'
                      ? "outcome unknown, check the broker's order book"
                      : o.status.toLowerCase()}
                    {o.broker_order_id ? ` (broker order ${o.broker_order_id})` : ''}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </CardContent>

      <AlertDialog open={engaging} onOpenChange={setEngaging}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Turn the kill switch on?</AlertDialogTitle>
            <AlertDialogDescription>
              Every live session closes its positions and ends now. Until you release the switch, no
              live session opens and only orders that close positions are sent.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Input
            aria-label="Reason"
            placeholder="Reason (optional)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => kill.mutate(true)}>
              Close every live position
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}
