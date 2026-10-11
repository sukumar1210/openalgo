// pages/research/RunLedger.tsx
// A run's ledger as one journal (every signal, target, order, fill, cost and
// P&L in sequence) and the checks that it reconciles. Phase 5 of the master PRD.

import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import {
  formatIstTime,
  formatRupees,
  getRunLedger,
  getRunReconciliation,
  LEDGER_KINDS,
  type LedgerEntry,
  type LedgerFilters,
  researchQueryKeys,
  runLedgerCsvUrl,
} from '@/api/research'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

export const KIND_LABEL: Record<string, string> = {
  SIGNAL: 'Signal',
  TARGET_POSITION: 'Target',
  SQUARE_OFF: 'Square-off',
  ORDER_INTENT: 'Order decided',
  ORDER_SUBMITTED: 'Order sent',
  ORDER_FILLED: 'Order filled',
  ORDER_REJECTED: 'Order refused',
  ORDER_UNFILLED: 'Not filled',
  FILL: 'Fill',
  FEE: 'Fee',
  SLIPPAGE: 'Slippage',
  POSITION_UPDATE: 'Position',
  REALIZED_PNL: 'Realized P&L',
  UNREALIZED_PNL: 'Unrealized P&L',
  SESSION_EVENT: 'Session',
  MARK: 'Mark',
}

const ALL = 'all'

function money(value: number | null): string {
  return value === null || value === undefined ? '' : formatRupees(value)
}

function price(entry: LedgerEntry): string {
  const value = entry.fill_price ?? entry.requested_price
  return value === null || value === undefined ? '' : value.toFixed(2)
}

function costs(entry: LedgerEntry): string {
  if (entry.fees === null && entry.slippage === null) return ''
  return formatRupees((entry.fees ?? 0) + (entry.slippage ?? 0))
}

export function RunLedger({ runId, labels }: { runId: number; labels: Map<number, string> }) {
  const [filters, setFilters] = useState<LedgerFilters>({ kinds: [], instance: null, symbol: '' })
  const [symbol, setSymbol] = useState('')

  const ledger = useInfiniteQuery({
    queryKey: researchQueryKeys.ledger(runId, filters),
    queryFn: ({ pageParam }) => getRunLedger(runId, filters, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })
  const entries = (ledger.data?.pages ?? []).flatMap((page) => page.items)

  return (
    <Card>
      <CardHeader className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle>Ledger</CardTitle>
            <CardDescription>
              Everything this run decided and every rupee it moved, in order. Each fill names the
              order it belongs to and the signal behind it.
            </CardDescription>
          </div>
          <Button asChild size="sm" variant="outline">
            <a href={runLedgerCsvUrl(runId, filters)} download>
              Download CSV
            </a>
          </Button>
        </div>
        <div className="flex flex-wrap gap-2">
          <Select
            value={filters.kinds[0] ?? ALL}
            onValueChange={(value) =>
              setFilters({ ...filters, kinds: value === ALL ? [] : [value] })
            }
          >
            <SelectTrigger className="w-44" aria-label="Entry">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All entries</SelectItem>
              {LEDGER_KINDS.map((kind) => (
                <SelectItem key={kind} value={kind}>
                  {KIND_LABEL[kind]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select
            value={filters.instance === null ? ALL : String(filters.instance)}
            onValueChange={(value) =>
              setFilters({ ...filters, instance: value === ALL ? null : Number(value) })
            }
          >
            <SelectTrigger className="w-56" aria-label="Strategy">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All strategies and orders</SelectItem>
              {[...labels.entries()].map(([instance, label]) => (
                <SelectItem key={instance} value={String(instance)}>
                  {label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <form
            className="flex gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              setFilters({ ...filters, symbol })
            }}
          >
            <Input
              className="w-36"
              placeholder="Symbol"
              aria-label="Symbol"
              value={symbol}
              onChange={(event) => setSymbol(event.target.value.toUpperCase())}
            />
            <Button type="submit" size="sm" variant="outline">
              Filter
            </Button>
          </form>
        </div>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        {ledger.error ? (
          <p className="text-sm text-destructive">{(ledger.error as Error).message}</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time (IST)</TableHead>
                <TableHead>Entry</TableHead>
                <TableHead>Strategy</TableHead>
                <TableHead>Symbol</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Price</TableHead>
                <TableHead className="text-right">Costs</TableHead>
                <TableHead className="text-right">P&amp;L</TableHead>
                <TableHead>Order</TableHead>
                <TableHead>Detail</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {entries.map((entry) => (
                <TableRow key={`${entry.seq}-${entry.kind}`}>
                  <TableCell className="whitespace-nowrap">
                    {entry.timestamp ? formatIstTime(entry.timestamp) : ''}
                  </TableCell>
                  <TableCell>{KIND_LABEL[entry.kind] ?? entry.kind}</TableCell>
                  <TableCell>
                    {entry.strategy_instance_id === null
                      ? 'Portfolio'
                      : (labels.get(entry.strategy_instance_id) ?? entry.strategy_instance_id)}
                  </TableCell>
                  <TableCell>{entry.symbol ?? ''}</TableCell>
                  <TableCell>
                    {entry.side === 'BUY' ? 'Buy' : entry.side === 'SELL' ? 'Sell' : ''}
                  </TableCell>
                  <TableCell className="text-right">{entry.quantity ?? ''}</TableCell>
                  <TableCell className="text-right">{price(entry)}</TableCell>
                  <TableCell className="text-right">{costs(entry)}</TableCell>
                  <TableCell className="text-right">{money(entry.net_pnl)}</TableCell>
                  <TableCell className="whitespace-nowrap font-mono text-xs">
                    {entry.platform_order_id?.split(':').pop() ?? ''}
                    {entry.execution_order_id ? ` (${entry.execution_order_id})` : ''}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{entry.detail}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        {!ledger.isLoading && entries.length === 0 && !ledger.hasNextPage && (
          <p className="py-4 text-center text-sm text-muted-foreground">Nothing matches.</p>
        )}
        {ledger.hasNextPage && (
          <div className="pt-3 text-center">
            <Button
              size="sm"
              variant="outline"
              disabled={ledger.isFetchingNextPage}
              onClick={() => ledger.fetchNextPage()}
            >
              {ledger.isFetchingNextPage ? 'Loading…' : 'Load more'}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function Reconciliation({ runId, live }: { runId: number; live: boolean }) {
  const report = useQuery({
    queryKey: researchQueryKeys.reconciliation(runId),
    queryFn: () => getRunReconciliation(runId),
    refetchInterval: live ? 10_000 : false,
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Reconciliation</CardTitle>
        <CardDescription>
          {report.data
            ? report.data.passed
              ? 'Every check the ledger must pass, passes.'
              : 'Some checks found differences. Each is listed below.'
            : 'Checking the ledger against itself.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {report.error && (
          <p className="text-sm text-destructive">{(report.error as Error).message}</p>
        )}
        {(report.data?.checks ?? []).map((check) => (
          <div key={check.name} className="rounded-md border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-sm font-medium">{check.name}</span>
              <Badge
                variant={
                  check.passed === null ? 'outline' : check.passed ? 'secondary' : 'destructive'
                }
              >
                {check.passed === null ? 'Not yet' : check.passed ? 'Passed' : 'Failed'}
              </Badge>
            </div>
            {check.detail && <p className="pt-1 text-xs text-muted-foreground">{check.detail}</p>}
            {check.problems.length > 0 && (
              <ul className="list-disc pt-1 pl-5 text-xs">
                {check.problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  )
}
