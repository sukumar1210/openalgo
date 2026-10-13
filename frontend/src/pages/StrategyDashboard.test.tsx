import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { getDashboard, getLedger, type StrategyPerformance } from '@/api/strategy-dashboard'
import StrategyDashboard from './StrategyDashboard'

vi.mock('@/components/portfolio/PortfolioLineChart', () => ({
  PortfolioLineChart: ({ series }: { series: { name: string }[] }) => (
    <ul aria-label="chart">
      {series.map((s) => (
        <li key={s.name}>{s.name}</li>
      ))}
    </ul>
  ),
}))

const summary = {
  total_pnl: 50,
  days: 2,
  winning_days: 1,
  best_day: 100,
  worst_day: -50,
  max_drawdown: -50,
  max_drawdown_pct: -0.05,
  trades: 4,
  last_day: '2026-10-09',
  open_positions: 0,
}
const row = (
  key: string,
  name: string,
  kind: StrategyPerformance['kind']
): StrategyPerformance => ({
  key,
  kind,
  name,
  tags: [name],
  summary,
  daily: [{ date: '2026-10-08', pnl: 100, trades: 2, cumulative: 100 }],
})
const strategies = [row('python:a', 'Alpha', 'python'), row('module:7', 'Iron fly', 'module')]

vi.mock('@/api/strategy-dashboard', async (original) => ({
  ...(await original<typeof import('@/api/strategy-dashboard')>()),
  getDashboard: vi.fn(async (_from: string, _to: string, combine: string[]) => ({
    capital: 100000,
    strategies,
    combined: combine.length
      ? {
          keys: combine,
          summary: { ...summary, total_pnl: 150 },
          daily: [{ date: '2026-10-08', pnl: 150, trades: 4, cumulative: 150 }],
          contributions: [{ key: 'python:a', name: 'Alpha', total_pnl: 50 }],
        }
      : null,
  })),
  getLedger: vi.fn(async () => [
    {
      time: '2026-10-09 15:00:00',
      order_name: 'AUTO_SQUARE_OFF',
      symbol: 'SBIN',
      exchange: 'NSE',
      product: 'MIS',
      action: 'SELL',
      quantity: 10,
      price: 95,
      square_off: true,
    },
  ]),
}))

function renderPage() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <StrategyDashboard />
    </QueryClientProvider>
  )
}

describe('StrategyDashboard', () => {
  it('draws each checked strategy, then adds the checked ones into one account', async () => {
    renderPage()
    const chart = await screen.findByRole('list', { name: 'chart' })
    expect(chart).toHaveTextContent('Alpha')
    expect(chart).toHaveTextContent('Iron fly')

    fireEvent.click(screen.getByRole('checkbox', { name: 'Show Iron fly' }))
    fireEvent.click(screen.getByRole('radio', { name: 'Combined' }))

    await waitFor(() => expect(getDashboard).toHaveBeenLastCalledWith('', '', ['python:a']))
    expect(await screen.findByText('Combined P&L')).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'chart' })).toHaveTextContent('Combined')
  })

  it("opens a strategy's ledger, square-off included", async () => {
    renderPage()
    await screen.findByText('Python strategy')
    fireEvent.click(screen.getAllByRole('button', { name: 'Ledger' })[0])

    expect(await screen.findByText('(square-off)')).toBeInTheDocument()
    expect(getLedger).toHaveBeenCalledWith('python:a')
  })
})
