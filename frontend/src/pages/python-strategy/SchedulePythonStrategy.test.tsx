import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import SchedulePythonStrategy from './SchedulePythonStrategy'

vi.mock('@/api/python-strategy', () => ({
  pythonStrategyApi: {
    getStrategy: vi.fn().mockResolvedValue({
      id: 'alpha_1',
      name: 'Alpha a adapter',
      status: 'running',
      exchange: 'NSE',
      schedule_start_time: '09:00',
      schedule_stop_time: '15:30',
      schedule_days: ['mon', 'tue'],
    }),
    getExchanges: vi.fn().mockResolvedValue([]),
  },
}))

describe('SchedulePythonStrategy', () => {
  it('stays a tab while the strategy runs, with the other tabs one click away', async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={['/python/alpha_1/schedule']}>
          <Routes>
            <Route path="/python/:strategyId/schedule" element={<SchedulePythonStrategy />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    )
    expect(
      await screen.findByText(/can't be changed while the strategy is running/)
    ).toBeInTheDocument()
    for (const name of ['Code', 'Logs', 'Backtest', 'Sandbox days', 'Results']) {
      expect(screen.getByRole('link', { name })).toHaveAttribute(
        'href',
        expect.stringMatching(/^\/python\/alpha_1\//)
      )
    }
    expect(screen.getByText(/It runs 09:00 to 15:30 IST on mon, tue/)).toBeInTheDocument()
  })
})
