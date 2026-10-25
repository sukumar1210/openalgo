// pages/runs/Accounts.tsx
// The Sandbox's money, account by account: the main account standalone
// strategies trade, each Run's accounts and each source's, what each is worth
// now, and every movement of money in or out that a trade did not make.

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { type AccountRow, getAccountEvents, getAccounts, runKeys } from '@/api/runs'
import { Badge } from '@/components/ui/badge'
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
import { cn } from '@/lib/utils'
import { Kpi, RunsNav, tone, useMoney } from './shared'

const KIND_LABEL: Record<AccountRow['kind'], string> = {
  member: "A strategy's, in a Run",
  run: "A shared Run's",
  source: 'A source',
}

const EVENT_LABEL: Record<string, string> = {
  open: 'Opened',
  deposit: 'Added',
  withdraw: 'Taken out',
  transfer: 'Moved by the P&L policy',
  charges: 'Charges on a fill',
  close: 'Closed',
}

export default function Accounts() {
  const money = useMoney()
  const overview = useQuery({ queryKey: runKeys.accounts(), queryFn: getAccounts })
  const [token, setToken] = useState<string | null>(null)
  const rows = overview.data?.accounts ?? []
  const chosen = rows.find((a) => a.token === token)

  return (
    <div className="container mx-auto space-y-6 px-4 py-6">
      <RunsNav />
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Accounts</h1>
        <p className="text-muted-foreground">
          Money of its own inside one Sandbox: a Run's strategies each trade an account (or a shared
          Run one between them), and each source trades its own. Strategies started from Strategies
          trade the main account.
        </p>
      </div>

      {overview.error && (
        <p className="text-sm text-destructive">
          The accounts could not be read just now. Refresh the page to try again.
        </p>
      )}
      {overview.data && (
        <div className="grid gap-4 sm:grid-cols-3">
          <Kpi
            label={overview.data.main.label}
            value={money(overview.data.main.equity ?? 0)}
            hint={`Starts at ${money(overview.data.main.starting_capital)}`}
          />
          <Kpi
            label="Accounts of Runs"
            value={rows.filter((a) => a.kind !== 'source' && a.status === 'active').length}
          />
          <Kpi
            label="Charges today, every account"
            value={money(rows.reduce((t, a) => t + a.charges_today, 0))}
          />
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Every account</CardTitle>
          <CardDescription>Choose one to see what moved its money.</CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          {!overview.isLoading && rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No accounts yet: a Run opens its accounts when it starts.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Account</TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead>Run</TableHead>
                  <TableHead className="text-right">Started with</TableHead>
                  <TableHead className="text-right">Worth now</TableHead>
                  <TableHead className="text-right">Charges today</TableHead>
                  <TableHead className="text-right">Daily loss limit</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((a) => {
                  const change = a.equity == null ? null : a.equity - a.starting_capital
                  return (
                    <TableRow key={a.token} className={cn(token === a.token && 'bg-muted/50')}>
                      <TableCell>
                        <div className="font-medium">{a.label}</div>
                        {a.status !== 'active' && <Badge variant="outline">Closed</Badge>}
                      </TableCell>
                      <TableCell className="text-sm">{KIND_LABEL[a.kind]}</TableCell>
                      <TableCell>
                        {a.run ? (
                          <Link to={`/runs/${a.run.id}`} className="hover:underline">
                            {a.run.name ?? `Run ${a.run.id}`}
                          </Link>
                        ) : (
                          '-'
                        )}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {money(a.starting_capital)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {a.equity == null ? '-' : money(a.equity)}
                        {change ? (
                          <div className={cn('text-xs', tone(change))}>{money(change)}</div>
                        ) : null}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {money(a.charges_today)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {a.daily_loss_limit == null ? '-' : money(a.daily_loss_limit)}
                      </TableCell>
                      <TableCell>
                        <Button
                          size="sm"
                          variant={token === a.token ? 'default' : 'outline'}
                          onClick={() => setToken(token === a.token ? null : a.token)}
                        >
                          Its money
                        </Button>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {chosen && <Movements account={chosen} />}
    </div>
  )
}

function Movements({ account }: { account: AccountRow }) {
  const money = useMoney()
  const events = useQuery({
    queryKey: runKeys.accountEvents(account.token),
    queryFn: () => getAccountEvents(account.token),
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>{account.label}: its money</CardTitle>
        <CardDescription>
          Newest first: opening, transfers by the P&L policy and the charges on its fills. A trade's
          own profit or loss is in its P&L, not here.
        </CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Day</TableHead>
              <TableHead>What</TableHead>
              <TableHead className="text-right">Amount</TableHead>
              <TableHead>Why</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {(events.data ?? []).map((e, i) => (
              <TableRow key={`${e.reference ?? ''}-${i}`}>
                <TableCell className="whitespace-nowrap">{e.date ?? '-'}</TableCell>
                <TableCell>{EVENT_LABEL[e.kind] ?? e.kind}</TableCell>
                <TableCell className={cn('text-right tabular-nums', tone(e.amount))}>
                  {money(e.amount)}
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">{e.reason ?? ''}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {events.isLoading && <p className="py-4 text-sm text-muted-foreground">Loading...</p>}
      </CardContent>
    </Card>
  )
}
