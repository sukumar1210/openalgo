// pages/research/RunLog.tsx
// A run's or session's log, the way Python Strategies shows a script's: the
// worker's lines, a strategy's tracebacks, and a script backtest's own output.

import { useQuery } from '@tanstack/react-query'
import { getRunLog, researchQueryKeys } from '@/api/research'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { LogViewer } from '@/components/ui/log-viewer'

export function RunLog({ runId, live }: { runId: number; live: boolean }) {
  const log = useQuery({
    queryKey: researchQueryKeys.log(runId),
    queryFn: () => getRunLog(runId),
    refetchInterval: live ? 5000 : false,
  })

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <div className="space-y-1.5">
          <CardTitle>Log</CardTitle>
          <CardDescription>
            {live
              ? 'What the run is doing, refreshed every 5 seconds.'
              : 'What the run did, including any error a strategy raised.'}
            {log.data?.truncated && ' Only the end of a long log is shown.'}
          </CardDescription>
        </div>
        <Button size="sm" variant="outline" onClick={() => log.refetch()}>
          Refresh
        </Button>
      </CardHeader>
      <CardContent>
        {log.error ? (
          <p className="text-sm text-destructive">{(log.error as Error).message}</p>
        ) : log.isLoading ? (
          <p className="py-6 text-center text-sm text-muted-foreground">Loading...</p>
        ) : log.data?.content ? (
          <LogViewer value={log.data.content} height="400px" followTail={live} />
        ) : (
          <p className="py-6 text-center text-sm text-muted-foreground">
            {log.data?.note ?? 'The log is empty.'}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
