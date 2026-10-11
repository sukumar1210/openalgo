// pages/research/HostedScriptsPanel.tsx
// The Python strategies hosted at /python, beside the Research strategies. Start
// and stop go through the strategy host's own endpoints, exactly as /python does;
// Research never runs a script itself. Adding one to Research records each day it
// trades in the Sandbox as a run, and lets it be backtested unchanged.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { pythonStrategyApi } from '@/api/python-strategy'
import {
  followScript,
  formatRupees,
  getHostedScripts,
  getOrderTags,
  type HostedScriptLink,
  researchQueryKeys,
} from '@/api/research'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import type { PythonStrategy } from '@/types/python-strategy'
import { STATUS_LABELS } from '@/types/python-strategy'
import type { ApiResponse } from '@/types/trading'
import { showToast } from '@/utils/toast'

const SCRIPTS_KEY = ['python-strategy', 'list'] as const

function schedule(script: PythonStrategy): string {
  if (!script.schedule_start_time) return 'No schedule'
  const days = script.schedule_days.map((d) => d.slice(0, 1).toUpperCase() + d.slice(1)).join(' ')
  return `${script.schedule_start_time} to ${script.schedule_stop_time ?? '-'}, ${days}`
}

function errorMessage(err: unknown, fallback: string): string {
  return (
    (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
    (err as Error)?.message ||
    fallback
  )
}

export function HostedScriptsPanel() {
  const queryClient = useQueryClient()
  const [following, setFollowing] = useState<PythonStrategy | null>(null)
  const scripts = useQuery({
    queryKey: SCRIPTS_KEY,
    queryFn: pythonStrategyApi.getStrategies,
    refetchInterval: 10_000,
  })
  const links = useQuery({
    queryKey: researchQueryKeys.scripts(),
    queryFn: getHostedScripts,
    refetchInterval: 60_000,
  })
  const act = useMutation({
    mutationFn: ({
      script,
      start,
    }: {
      script: PythonStrategy
      start: boolean
    }): Promise<ApiResponse<unknown>> =>
      start
        ? pythonStrategyApi.startStrategy(script.id)
        : pythonStrategyApi.stopStrategy(script.id),
    onSuccess: (response, { script, start }) => {
      if (response.status === 'success') {
        showToast.success(response.message || `${script.name} ${start ? 'started' : 'stopped'}`)
      } else {
        showToast.error(response.message || `${script.name} could not be changed`)
      }
      queryClient.invalidateQueries({ queryKey: SCRIPTS_KEY })
    },
    onError: (err) =>
      showToast.error(errorMessage(err, 'The Python strategy could not be changed')),
  })

  const rows = scripts.data ?? []

  return (
    <Card>
      <CardHeader>
        <CardTitle>Python strategies</CardTitle>
        <CardDescription>
          Scripts hosted in{' '}
          <Link to="/python" className="text-primary hover:underline">
            Python Strategies
          </Link>
          , running on their own schedule. Add one to Research to record each day it trades in the
          Sandbox as a run, and to backtest the script unchanged on stored market data.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {scripts.error ? (
          <p className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
            {errorMessage(scripts.error, 'The Python strategies could not be loaded')}
          </p>
        ) : scripts.isLoading ? (
          <p className="py-6 text-center text-sm text-muted-foreground">Loading...</p>
        ) : rows.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            No Python strategies yet.{' '}
            <Link to="/python/new" className="text-primary hover:underline">
              Upload one
            </Link>
            .
          </p>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Python strategy</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Schedule</TableHead>
                  <TableHead>In Research</TableHead>
                  <TableHead>Last recorded day</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((script) => {
                  const link: HostedScriptLink | undefined = links.data?.[script.id]
                  const running = script.status === 'running'
                  const scheduled = script.status === 'scheduled'
                  return (
                    <TableRow key={script.id}>
                      <TableCell>
                        <div className="font-medium">{script.name}</div>
                        <div className="text-xs text-muted-foreground">{script.file_name}</div>
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant={running ? 'default' : 'outline'}
                          title={script.status_message || undefined}
                        >
                          {STATUS_LABELS[script.status] ?? script.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-sm">{schedule(script)}</TableCell>
                      <TableCell className="text-sm">
                        {link ? (
                          <div>
                            <Link
                              to={`/research/strategies/${link.strategy_id}`}
                              className="text-primary hover:underline"
                            >
                              Open in Research
                            </Link>
                            <div className="text-xs text-muted-foreground">
                              {link.order_tag
                                ? `Orders tagged "${link.order_tag}", ${link.trades_today} trades today`
                                : 'No order tag: its trading is not recorded'}
                            </div>
                          </div>
                        ) : (
                          <span className="text-muted-foreground">Not added</span>
                        )}
                      </TableCell>
                      <TableCell className="text-sm">
                        {link?.last_recorded ? (
                          <Link
                            to={`/research/runs/${link.last_recorded.run_id}`}
                            className="hover:underline"
                          >
                            {link.last_recorded.net_pnl == null
                              ? '-'
                              : formatRupees(link.last_recorded.net_pnl)}{' '}
                            <span className="text-xs text-muted-foreground">
                              {link.last_recorded.trading_date}
                            </span>
                          </Link>
                        ) : (
                          <span className="text-muted-foreground">-</span>
                        )}
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap justify-end gap-1">
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={act.isPending}
                            title={
                              scheduled
                                ? "Cancel today's scheduled start"
                                : running
                                  ? 'Stop the running script'
                                  : 'Start the script now'
                            }
                            onClick={() => act.mutate({ script, start: !(running || scheduled) })}
                          >
                            {running ? 'Stop' : scheduled ? 'Cancel start' : 'Start'}
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => setFollowing(script)}>
                            {link ? 'Order tag' : 'Add to Research'}
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
      {following && (
        <FollowScriptDialog
          script={following}
          link={links.data?.[following.id]}
          onClose={() => setFollowing(null)}
        />
      )}
    </Card>
  )
}

function FollowScriptDialog({
  script,
  link,
  onClose,
}: {
  script: PythonStrategy
  link: HostedScriptLink | undefined
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [tag, setTag] = useState(link?.order_tag ?? '')
  const [capital, setCapital] = useState('500000')
  const [universe, setUniverse] = useState('')
  const tags = useQuery({ queryKey: researchQueryKeys.orderTags(), queryFn: getOrderTags })
  const follow = useMutation({
    mutationFn: () =>
      followScript(script.id, {
        order_tag: tag.trim() || null,
        ...(link
          ? {}
          : {
              capital: Number(capital),
              universe: universe
                .split(/[\s,]+/)
                .map((s) => s.trim().toUpperCase())
                .filter(Boolean),
            }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: researchQueryKeys.all })
      showToast.success(link ? 'Order tag saved' : `${script.name} added to Research`)
      onClose()
    },
    onError: (err: Error) => showToast.error(err.message),
  })

  const choose = (name: string) => {
    setTag(name)
    const known = tags.data?.find((t) => t.tag === name)
    if (known && !universe.trim()) setUniverse(known.symbols.join(', '))
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{link ? `Order tag for ${script.name}` : `Add ${script.name}`}</DialogTitle>
          <DialogDescription>
            Research finds this script's Sandbox trading by the strategy name its orders send. Each
            day it trades is recorded as a run once the script has stopped for the day. The script
            keeps running from Python Strategies exactly as before.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="follow-tag">Strategy name on its orders</Label>
            <Input
              id="follow-tag"
              value={tag}
              placeholder="For example EMA Crossover Python"
              onChange={(e) => setTag(e.target.value)}
            />
            {(tags.data?.length ?? 0) > 0 && (
              <div className="flex flex-wrap gap-1 pt-1">
                {tags.data?.map((t) => (
                  <Button
                    key={t.tag}
                    type="button"
                    size="sm"
                    variant={t.tag === tag ? 'default' : 'outline'}
                    onClick={() => choose(t.tag)}
                  >
                    {t.tag} ({t.orders})
                  </Button>
                ))}
              </div>
            )}
            <p className="text-xs text-muted-foreground">
              The names above are the ones the Sandbox's orders already carry, with how many orders
              each has. Leave it empty to backtest the script without recording its trading.
            </p>
          </div>
          {!link && (
            <>
              <div className="space-y-1.5">
                <Label htmlFor="follow-capital">Capital (Rs)</Label>
                <Input
                  id="follow-capital"
                  type="number"
                  min={1}
                  value={capital}
                  onChange={(e) => setCapital(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="follow-universe">Symbols it trades</Label>
                <Textarea
                  id="follow-universe"
                  rows={3}
                  value={universe}
                  placeholder="SBIN, INFY, RELIANCE"
                  onChange={(e) => setUniverse(e.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  A backtest loads these symbols' bars for the script to read. Its start and stop
                  times come from the script's schedule; both can be changed later as a new
                  configuration.
                </p>
              </div>
            </>
          )}
        </div>
        <DialogFooter>
          <Button disabled={follow.isPending} onClick={() => follow.mutate()}>
            {link ? 'Save' : 'Add to Research'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
