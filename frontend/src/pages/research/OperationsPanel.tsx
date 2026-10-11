// pages/research/OperationsPanel.tsx
// Operations, from the Research page: every worker process the app is running
// (backtests and sessions), with its memory, how long it has run, its last
// heartbeat and lease, and one control to stop every session at once.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import {
  ENVIRONMENT_LABEL,
  getOperations,
  researchQueryKeys,
  type SessionEnvironment,
  stopAllSessions,
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
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { showToast } from '@/utils/toast'

function duration(seconds: number | null): string {
  if (seconds === null) return '-'
  const minutes = Math.floor(seconds / 60)
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`
}

export function OperationsPanel() {
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const { data } = useQuery({
    queryKey: researchQueryKeys.operations(),
    queryFn: getOperations,
    refetchInterval: 10_000,
  })
  const stopAll = useMutation({
    mutationFn: stopAllSessions,
    onSuccess: (stopped) => {
      queryClient.invalidateQueries({ queryKey: researchQueryKeys.all })
      showToast.success(
        stopped.length
          ? `Stopping ${stopped.length} session(s): each is closing its positions`
          : 'No session was running'
      )
    },
    onError: (err: Error) => showToast.error(err.message),
  })

  if (!data) return null
  const sessions = data.workers.filter((w) => w.kind === 'session').length
  const backtests = data.workers.length - sessions

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <div>
          <CardTitle>Operations</CardTitle>
          <CardDescription>
            {backtests} of {data.limits.backtests} backtests and {sessions} of{' '}
            {data.limits.sessions} sessions running. Each worker may use up to{' '}
            {data.limits.memory_mb} MB; a backtest stops after{' '}
            {Math.round(data.limits.job_timeout_seconds / 60)} minutes.
          </CardDescription>
        </div>
        <Button
          variant="outline"
          disabled={sessions === 0 || stopAll.isPending}
          onClick={() => setConfirming(true)}
        >
          Stop all sessions
        </Button>
      </CardHeader>
      <CardContent>
        {data.workers.length === 0 ? (
          <p className="text-sm text-muted-foreground">No worker is running.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Memory</TableHead>
                <TableHead className="text-right">Running for</TableHead>
                <TableHead>Last heartbeat (UTC)</TableHead>
                <TableHead className="text-right">Restarts</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.workers.map((w) => (
                <TableRow key={w.run_id}>
                  <TableCell>
                    <Link
                      to={
                        w.kind === 'session'
                          ? `/research/sessions/${w.run_id}`
                          : `/research/runs/${w.run_id}`
                      }
                      className="hover:underline"
                    >
                      #{w.run_id}
                    </Link>
                  </TableCell>
                  <TableCell>
                    {w.kind === 'session'
                      ? `${ENVIRONMENT_LABEL[w.environment as SessionEnvironment] ?? 'Sandbox'} session`
                      : 'Backtest'}
                  </TableCell>
                  <TableCell>{w.status?.toLowerCase() ?? '-'}</TableCell>
                  <TableCell className="text-right">
                    {w.memory_mb === null ? '-' : `${w.memory_mb} MB`}
                  </TableCell>
                  <TableCell className="text-right">{duration(w.running_seconds)}</TableCell>
                  <TableCell>{w.heartbeat_at?.slice(11, 19) ?? '-'}</TableCell>
                  <TableCell className="text-right">{w.restarts}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>

      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Stop every session?</AlertDialogTitle>
            <AlertDialogDescription>
              Every Sandbox, Shadow and Live session closes its positions and finishes for today.
              Deployments stay as they are, so tomorrow's sessions open as scheduled.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => stopAll.mutate()}>
              Stop all sessions
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}
