// pages/runs/shared.tsx
// What the Runs pages share: their tabs, how a figure is coloured, a Run's
// status, its totals for today, and its history told in sentences.

import { type ReactNode, useMemo } from 'react'
import { Link, useLocation } from 'react-router'
import type { Run, RunEvent, RunStatus } from '@/api/runs'
import { STATUS_LABEL } from '@/api/runs'
import { Badge } from '@/components/ui/badge'
import { cn, makeFormatCurrency } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'

const TABS = [
  { href: '/python', label: 'Strategies' },
  { href: '/runs', label: 'Runs' },
  { href: '/runs/pnl', label: 'P&L' },
  { href: '/runs/compare', label: 'Compare' },
  { href: '/runs/accounts', label: 'Accounts' },
]

/** Strategies keeps the scripts; a Run trades some of them; P&L reads them all. */
export function RunsNav() {
  const { pathname } = useLocation()
  const current = TABS.filter((t) => pathname === t.href || pathname.startsWith(`${t.href}/`)).sort(
    (a, b) => b.href.length - a.href.length
  )[0]?.href
  return (
    <nav aria-label="Runs" className="flex flex-wrap gap-1 border-b pb-2">
      {TABS.map((tab) => (
        <Link
          key={tab.href}
          to={tab.href}
          aria-current={current === tab.href ? 'page' : undefined}
          className={cn(
            'rounded-md px-3 py-1.5 text-sm font-medium',
            current === tab.href
              ? 'bg-primary text-primary-foreground'
              : 'text-muted-foreground hover:bg-accent hover:text-foreground'
          )}
        >
          {tab.label}
        </Link>
      ))}
    </nav>
  )
}

/** Green for a gain, red for a loss, nothing for zero or no figure. */
export function tone(value: number | null | undefined): string {
  if (!value) return ''
  return value > 0 ? 'text-green-600 dark:text-green-500' : 'text-red-600 dark:text-red-500'
}

export function useMoney() {
  const { user } = useAuthStore()
  return useMemo(() => makeFormatCurrency(user?.broker), [user?.broker])
}

export function StatusBadge({ status }: { status: RunStatus }) {
  const variant = status === 'active' ? 'default' : status === 'paused' ? 'secondary' : 'outline'
  return <Badge variant={variant}>{STATUS_LABEL[status]}</Badge>
}

/** A Run's totals today: capital, the day so far, what it holds against its limits. */
export function runToday(run: Run) {
  const seats = run.today.seats
  const opened = run.today.opened
  const sum = (pick: (s: (typeof seats)[number]) => number | null) =>
    seats.reduce((total, s) => total + (pick(s) ?? 0), 0)
  return {
    opened,
    capital: opened ? sum((s) => s.opening) : run.version.capital,
    net: opened ? sum((s) => s.net) : null,
    charges: opened ? sum((s) => s.charges) : null,
    exposure: sum((s) => s.exposure),
    limit: opened ? sum((s) => s.limit) : null,
    stopped: seats.filter((s) => s.stopped).map((s) => s.label),
  }
}

/** Its net since it started: every booked day, and today as it stands. */
export function netSinceStart(run: Run): number | null {
  if (!run.days.length) return null
  const booked = run.days.filter((d) => d.closed).reduce((t, d) => t + (d.net ?? 0), 0)
  const open = run.days.some((d) => !d.closed && d.trade_date === run.today.date)
  return booked + (open ? (runToday(run).net ?? 0) : 0)
}

export function firstDay(run: Run): string | null {
  return run.days.length ? run.days[0].trade_date : null
}

/** What happened to a Run, in words. */
export function describeEvent(event: RunEvent): string {
  const d = event.detail as Record<string, string | number | undefined>
  switch (event.kind) {
    case 'created':
      return 'Defined'
    case 'edited':
      return `Edit saved; it applies from ${d.effective_from}`
    case 'started':
      return 'Started'
    case 'paused':
      return 'Paused: no new copies; what the accounts hold stays until the square-off'
    case 'killed':
      return 'Kill switch: paused and everything the accounts held was closed'
    case 'stopped':
      return 'Stopped for good; the accounts keep their history'
    case 'day_opened':
      return `Day ${d.day} opened on version ${d.version}`
    case 'backtest':
      return `Backtest asked for ${d.first} to ${d.last}`
    default:
      return event.kind.replace(/_/g, ' ')
  }
}

/** A time as the trader reads it: IST, to the minute. */
export function istTime(iso: string | null): string {
  if (!iso) return '-'
  const stamp = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`)
  return stamp.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

export function Kpi({
  label,
  value,
  className,
  hint,
}: {
  label: string
  value: ReactNode
  className?: string
  hint?: ReactNode
}) {
  return (
    <div className="rounded-lg border bg-card p-4">
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className={cn('mt-1 text-xl font-semibold tabular-nums', className)}>{value}</p>
      {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
    </div>
  )
}
