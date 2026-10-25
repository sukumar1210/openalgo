import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { actOnRun, type Run } from '@/api/runs'
import RunDetail from './RunDetail'
import RunsIndex from './RunsIndex'

vi.mock('./PnlReport', () => ({ PnlReport: () => <p>report</p>, COLORS: ['#000'] }))
vi.mock('./BacktestPanel', () => ({ BacktestPanel: () => <p>backtests</p> }))

const version = {
  number: 1,
  effective_from: '2026-10-15',
  capital: 500000,
  mode: 'isolated',
  policy: 'own',
  limit: 'capital',
  loss_limit_pct: 2,
  members: [
    {
      id: 1,
      seat: 'a',
      label: 'Alpha',
      script_id: 'alpha',
      allocation_kind: 'fixed',
      allocation_value: 300000,
      allocated: 300000,
      sizing: 'platform',
      capital_dependent: false,
      fingerprint: 'f1',
      order_name: 'Trial / Alpha',
      settings: {
        exchange: 'NSE',
        schedule: { start: '09:16', stop: '15:10', days: [] },
        symbols: [],
        script_name: 'Alpha',
        file_name: 'alpha.py',
        code_hash: 'abcdef1234567',
      },
    },
  ],
}
const run = (id: number, name: string): Run =>
  ({
    id,
    name,
    status: 'active',
    version,
    upcoming: { ...version, number: 2, effective_from: '2026-10-16' },
    accounts: { a: 'u~1' },
    days: [
      {
        seat: 'a',
        trade_date: '2026-10-15',
        opening_capital: 300000,
        transfer: 0,
        limit: 300000,
        closing_balance: null,
        gross: null,
        charges: null,
        net: null,
        trades: null,
        closed: false,
      },
    ],
    today: {
      date: '2026-10-15',
      opened: true,
      seats: [
        {
          seat: 'a',
          label: 'Alpha',
          opening: 300000,
          transfer: 0,
          limit: 300000,
          loss_limit: 6000,
          stopped: false,
          net: -120,
          charges: 40,
          trades: 2,
          exposure: 3000,
          positions: [],
        },
      ],
      members: [{ seat: 'a', label: 'Alpha', capital: 300000 }],
    },
    copies: [
      {
        time: '10:00:00',
        member: 'Alpha',
        seat: 'a',
        symbol: 'SBIN',
        action: 'BUY',
        quantity: 30,
        price: 100,
        status: 'placed',
        reason: null,
      },
      {
        time: '11:02:40',
        member: 'Alpha',
        seat: 'a',
        symbol: 'SBIN',
        action: 'BUY',
        quantity: 10,
        price: 101,
        status: 'refused',
        reason: 'past its limit',
      },
    ],
    sources: [
      {
        name: 'Alpha #f1',
        fingerprint: 'f1',
        status: 'active',
        running: true,
        reference_capital: 10_000_000,
        schedule: null,
        runs: [name],
      },
    ],
    events: [
      { kind: 'day_opened', detail: { day: '2026-10-15', version: 1 }, at: '2026-10-15T03:35:00' },
    ],
  }) as unknown as Run

vi.mock('@/api/runs', async (original) => ({
  ...(await original<typeof import('@/api/runs')>()),
  getRun: vi.fn(async (id: number) => run(id, 'Trial')),
  listRuns: vi.fn(async () => [run(1, 'Trial'), run(2, 'Trial two')]),
  getSources: vi.fn(async () => []),
  actOnRun: vi.fn(async (id: number) => run(id, 'Trial')),
}))

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/runs" element={<RunsIndex />} />
          <Route path="/runs/:runId" element={<RunDetail />} />
          <Route path="/runs/compare" element={<p>Compare page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

afterEach(() => vi.clearAllMocks())

describe('RunDetail', () => {
  it("shows today's copies, a refusal with its reason, and the upcoming edit", async () => {
    renderAt('/runs/1')
    expect(
      await screen.findByText('Refused: past its limit. The next trade catches up.')
    ).toBeInTheDocument()
    expect(screen.getByText("Filled at the source's price")).toBeInTheDocument()
    expect(screen.getByText(/An edit applies from 2026-10-16 \(version 2\)/)).toBeInTheDocument()
    const row = screen.getByRole('row', { name: /^Alpha/ })
    expect(within(row).getByText('1 filled · 1 refused')).toBeInTheDocument()
    expect(within(row).getByText('Alpha #f1 at 3.00%')).toBeInTheDocument()
  })

  it('asks before the kill switch closes everything', async () => {
    renderAt('/runs/1')
    fireEvent.click(await screen.findByRole('button', { name: 'Kill switch' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(actOnRun).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Kill switch' }))
    await waitFor(() => expect(actOnRun).toHaveBeenCalledWith(1, 'kill'))
  })
})

describe('RunsIndex', () => {
  it('compares the Runs ticked, two or more', async () => {
    renderAt('/runs')
    const compare = await screen.findByRole('button', { name: 'Compare selected' })
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Compare Trial' }))
    expect(compare).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Compare Trial two' }))
    fireEvent.click(compare)
    expect(await screen.findByText('Compare page')).toBeInTheDocument()
  })
})
