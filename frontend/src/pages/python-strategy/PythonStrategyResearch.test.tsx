import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import PythonStrategyResearch, { type ResearchView } from './PythonStrategyResearch'

const api = vi.hoisted(() => ({ getScriptOverview: vi.fn(), backtestScript: vi.fn() }))
vi.mock('@/api/research', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/research')>()),
  getScriptOverview: api.getScriptOverview,
  backtestScript: api.backtestScript,
}))

const row = (overrides = {}) => ({
  id: 7,
  status: 'COMPLETED',
  message: null,
  progress: 1,
  trading_date: null,
  from: '2026-09-01',
  to: '2026-09-30',
  net_pnl: 1250,
  return_pct: 0.25,
  max_drawdown_pct: 1.5,
  trades: 4,
  win_rate_pct: 50,
  queued_at: null,
  finished_at: null,
  ...overrides,
})

const OVERVIEW = {
  script_id: 'alpha_1',
  name: 'Alpha a adapter',
  strategy_id: 3,
  settings: {
    capital: 500000,
    leverage: 5,
    entry_time: '09:00',
    exit_time: '15:30',
    exchange: 'NSE',
    days: ['mon', 'tue', 'wed', 'thu', 'fri'],
  },
  order_tags: ['alpha_a_paper_obs_1'],
  backtests: [row()],
  sandbox_days: [row({ id: 9, trading_date: '2026-10-09', net_pnl: -300 })],
  summary: {
    days: 1,
    net_pnl: -300,
    winning_days: 0,
    trades: 1,
    best_day: -300,
    worst_day: -300,
  },
}

function show(view: ResearchView) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[`/python/alpha_1/${view}`]}>
        <Routes>
          <Route
            path={`/python/:strategyId/${view}`}
            element={<PythonStrategyResearch view={view} />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('PythonStrategyResearch', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getScriptOverview.mockResolvedValue(OVERVIEW)
  })

  it("backtests on the script's own settings, asking only for dates", async () => {
    api.backtestScript.mockResolvedValue({ id: 8, created: true })
    show('backtest')
    expect(await screen.findByText(/09:00 to 15:30, Mon Tue Wed Thu Fri, NSE/)).toBeInTheDocument()
    expect(screen.queryByLabelText(/Capital|Symbols/)).toBeNull()
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-09-01' } })
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-09-30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }))
    await waitFor(() =>
      expect(api.backtestScript).toHaveBeenCalledWith('alpha_1', '2026-09-01', '2026-09-30')
    )
    expect(screen.getByRole('link', { name: 'Open' })).toHaveAttribute('href', '/research/runs/7')
  })

  it('lists the days it traded, recognised by the names its orders carry', async () => {
    show('sandbox')
    expect(await screen.findByText(/alpha_a_paper_obs_1/)).toBeInTheDocument()
    expect(screen.getByText('2026-10-09')).toBeInTheDocument()
  })

  it('sets the Sandbox days beside the latest backtest', async () => {
    show('results')
    expect(await screen.findByText('In the Sandbox')).toBeInTheDocument()
    expect(screen.getByText('Latest backtest')).toBeInTheDocument()
    expect(screen.getByText('1 (0 winning)')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/paper|\barm(ed|ing)?\b/i)
  })

  it("shows the strategy's tabs, from its code to its results", async () => {
    show('backtest')
    for (const name of ['Code', 'Logs', 'Schedule', 'Backtest', 'Sandbox days', 'Results']) {
      expect(await screen.findByRole('link', { name })).toBeInTheDocument()
    }
  })
})
