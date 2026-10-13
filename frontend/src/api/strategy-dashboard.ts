// api/strategy-dashboard.ts
// Every strategy's Sandbox trading, from blueprints/strategy_dashboard.py.

import { webClient } from './client'

export type StrategyKind = 'python' | 'module' | 'other'

export interface DailyPnl {
  date: string
  pnl: number
  trades: number
  cumulative: number
}

export interface PerformanceSummary {
  total_pnl: number
  days: number
  winning_days: number
  best_day: number | null
  worst_day: number | null
  max_drawdown: number
  max_drawdown_pct: number | null
  trades: number
  last_day: string | null
  open_positions?: number
}

export interface StrategyPerformance {
  key: string
  kind: StrategyKind
  name: string
  /** The order names that are this strategy's own. */
  tags: string[]
  summary: PerformanceSummary
  daily: DailyPnl[]
}

export interface CombinedPerformance {
  keys: string[]
  summary: PerformanceSummary
  daily: DailyPnl[]
  contributions: { key: string; name: string; total_pnl: number }[]
}

export interface DashboardData {
  capital: number
  strategies: StrategyPerformance[]
  combined: CombinedPerformance | null
}

export interface LedgerFill {
  time: string
  order_name: string
  symbol: string
  exchange: string
  product: string
  action: 'BUY' | 'SELL'
  quantity: number
  price: number
  /** The sandbox's own end-of-day square-off, given to this strategy for what it held. */
  square_off: boolean
}

export const strategyDashboardKeys = {
  data: (from: string, to: string, combine: string[]) =>
    ['strategy-dashboard', 'data', from, to, combine.join(',')] as const,
  ledger: (key: string) => ['strategy-dashboard', 'ledger', key] as const,
}

export async function getDashboard(from: string, to: string, combine: string[]) {
  const params = new URLSearchParams()
  if (from) params.set('from', from)
  if (to) params.set('to', to)
  if (combine.length) params.set('combine', combine.join(','))
  const response = await webClient.get<{ data: DashboardData }>(
    `/strategy-dashboard/api/data?${params}`
  )
  return response.data.data
}

export async function getLedger(key: string) {
  const response = await webClient.get<{ data: LedgerFill[] }>(
    `/strategy-dashboard/api/ledger?key=${encodeURIComponent(key)}`
  )
  return response.data.data
}
