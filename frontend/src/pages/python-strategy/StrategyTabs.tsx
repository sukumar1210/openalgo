// pages/python-strategy/StrategyTabs.tsx
// One Python strategy's pages, as tabs: its code, logs and schedule from the
// strategy host, and what Research adds to the same script (backtests, the days
// it traded in the Sandbox, and their results). Each tab is its own route, so a
// bookmark or a refresh lands on the same tab.

import { NavLink } from 'react-router'
import { cn } from '@/lib/utils'

const TABS = [
  { path: 'edit', label: 'Code' },
  { path: 'logs', label: 'Logs' },
  { path: 'schedule', label: 'Schedule' },
  { path: 'backtest', label: 'Backtest' },
  { path: 'sandbox', label: 'Sandbox days' },
  { path: 'results', label: 'Results' },
] as const

export function StrategyTabs({ strategyId }: { strategyId: string }) {
  return (
    <nav aria-label="Strategy pages" className="flex gap-1 overflow-x-auto border-b">
      {TABS.map((tab) => (
        <NavLink
          key={tab.path}
          to={`/python/${strategyId}/${tab.path}`}
          className={({ isActive }) =>
            cn(
              '-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium transition-colors',
              isActive
                ? 'border-primary text-foreground'
                : 'border-transparent text-muted-foreground hover:text-foreground'
            )
          }
        >
          {tab.label}
        </NavLink>
      ))}
    </nav>
  )
}
