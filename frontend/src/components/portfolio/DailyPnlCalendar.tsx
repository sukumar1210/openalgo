/**
 * A month of trading days, each coloured by that day's net P&L after charges.
 *
 * A trader checks a strategy by its days: which ones lost, and whether the
 * bad ones bunch together. The colour scale is fixed to every day given, as in
 * WeeklyReturnsHeatmap, so a quiet month is not painted as dramatic simply
 * because it is the one being viewed. Clicking a day hands it to the page,
 * which opens that day's detail.
 */
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export interface CalendarDay {
  date: string
  net: number
  opening?: number
  charges?: number
  trades?: number
}

interface Props {
  days: CalendarDay[]
  format: (v: number) => string
  selected?: string | null
  onSelect?: (date: string) => void
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const MONTHS = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

const iso = (year: number, month: number, day: number) =>
  `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`

/** The background for a day's net, scaled to the largest day given. */
export function dayColor(net: number, extent: number): string {
  if (!net) return 'transparent'
  const a = Math.min(Math.abs(net) / extent, 1) * 0.85 + 0.1
  return net > 0 ? `rgba(34,197,94,${a.toFixed(3)})` : `rgba(239,68,68,${a.toFixed(3)})`
}

export function DailyPnlCalendar({ days, format, selected, onSelect }: Props) {
  const byDate = useMemo(() => new Map(days.map((d) => [d.date, d])), [days])
  const extent = Math.max(...days.map((d) => Math.abs(d.net)), 1)
  const last = days.length ? days[days.length - 1].date : new Date().toISOString().slice(0, 10)
  const [cursor, setCursor] = useState({
    year: Number(last.slice(0, 4)),
    month: Number(last.slice(5, 7)) - 1,
  })
  const years = useMemo(() => {
    const seen = new Set(days.map((d) => Number(d.date.slice(0, 4))))
    seen.add(cursor.year)
    return [...seen].sort()
  }, [days, cursor.year])

  const { year, month } = cursor
  const count = new Date(Date.UTC(year, month + 1, 0)).getUTCDate()
  // Monday first: getUTCDay() is 0 for Sunday.
  const lead = (new Date(Date.UTC(year, month, 1)).getUTCDay() + 6) % 7
  const prefix = iso(year, month, 1).slice(0, 7)
  const monthTotal = days.filter((d) => d.date.startsWith(prefix)).reduce((t, d) => t + d.net, 0)
  const yearTotal = days.filter((d) => d.date.startsWith(`${year}-`)).reduce((t, d) => t + d.net, 0)

  const step = (by: number) =>
    setCursor(({ year: y, month: m }) => {
      const next = m + by
      return { year: y + Math.floor(next / 12), month: ((next % 12) + 12) % 12 }
    })

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="icon"
            aria-label="Previous month"
            onClick={() => step(-1)}
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <h3 className="min-w-36 text-center font-semibold" aria-live="polite">
            {MONTHS[month]} {year}
          </h3>
          <Button variant="outline" size="icon" aria-label="Next month" onClick={() => step(1)}>
            <ChevronRight className="h-4 w-4" />
          </Button>
          <select
            aria-label="Year"
            value={year}
            onChange={(e) => setCursor({ year: Number(e.target.value), month })}
            className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
          >
            {years.map((y) => (
              <option key={y} value={y}>
                {y}
              </option>
            ))}
          </select>
        </div>
        <div className="flex gap-4 text-sm">
          <span>
            <span className="text-muted-foreground">Month </span>
            <span className={cn('font-medium tabular-nums', tone(monthTotal))}>
              {format(monthTotal)}
            </span>
          </span>
          <span>
            <span className="text-muted-foreground">Year </span>
            <span className={cn('font-medium tabular-nums', tone(yearTotal))}>
              {format(yearTotal)}
            </span>
          </span>
        </div>
      </div>

      <div className="grid grid-cols-7 gap-1.5 text-center text-xs text-muted-foreground">
        {WEEKDAYS.map((d) => (
          <span key={d}>{d}</span>
        ))}
      </div>
      <div className="grid grid-cols-7 gap-1.5">
        {Array.from({ length: lead }, (_, i) => (
          <span key={`lead-${i}`} aria-hidden="true" />
        ))}
        {Array.from({ length: count }, (_, i) => {
          const date = iso(year, month, i + 1)
          const weekday = (lead + i) % 7
          const day = byDate.get(date)
          if (!day) {
            return (
              <div
                key={date}
                className={cn(
                  'min-h-16 rounded-md border border-border/40 p-1.5 text-left text-xs',
                  weekday >= 5 ? 'bg-muted text-muted-foreground' : 'text-muted-foreground'
                )}
              >
                {i + 1}
              </div>
            )
          }
          const pct = day.opening ? (day.net / day.opening) * 100 : null
          const label = [
            `${date}: net ${format(day.net)}`,
            pct === null ? null : `${pct.toFixed(2)}% of opening capital`,
            day.charges === undefined ? null : `charges ${format(day.charges)}`,
            day.trades === undefined ? null : `${day.trades} trades`,
          ]
            .filter(Boolean)
            .join(', ')
          const strong = Math.abs(day.net) / extent > 0.6
          return (
            <button
              key={date}
              type="button"
              title={label}
              aria-label={label}
              aria-pressed={selected === date}
              onClick={() => onSelect?.(date)}
              className={cn(
                'flex min-h-16 flex-col justify-between rounded-md border p-1.5 text-left text-xs',
                selected === date ? 'border-primary ring-2 ring-primary' : 'border-border/40',
                strong && 'text-white'
              )}
              style={{ background: dayColor(day.net, extent) }}
            >
              <span className="font-semibold">{i + 1}</span>
              <span className="truncate tabular-nums">{format(day.net)}</span>
            </button>
          )
        })}
      </div>
      <p className="text-xs text-muted-foreground">
        Green a gain and red a loss, deeper for a larger day across every day shown. Weekends are
        grey; a weekday without a figure had no trading.
      </p>
    </div>
  )
}

function tone(value: number) {
  if (!value) return ''
  return value > 0 ? 'text-green-600 dark:text-green-500' : 'text-red-600 dark:text-red-500'
}
