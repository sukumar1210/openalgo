import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { actOnRun, createRun } from '@/api/runs'
import RunForm, { dayTwo } from './RunForm'

vi.mock('@/api/python-strategy', () => ({
  pythonStrategyApi: {
    getStrategies: vi.fn(async () => [
      {
        id: 'alpha',
        name: 'Alpha A',
        exchange: 'NSE',
        schedule_start_time: '09:16',
        schedule_stop_time: '15:10',
        symbol_list: { id: 'l', name: 'Nifty', count: 49 },
      },
      {
        id: 'res',
        name: 'RES-1',
        exchange: 'NSE',
        schedule_start_time: '09:20',
        schedule_stop_time: null,
        symbol_list: null,
      },
      {
        id: 'gold',
        name: 'Gold',
        exchange: 'MCX',
        schedule_start_time: '09:00',
        schedule_stop_time: null,
        symbol_list: null,
      },
    ]),
  },
}))

vi.mock('@/api/runs', async (original) => ({
  ...(await original<typeof import('@/api/runs')>()),
  getAccounts: vi.fn(async () => ({
    main: { label: 'Main Sandbox account', starting_capital: 500000, equity: 500000 },
    accounts: [],
  })),
  createRun: vi.fn(async () => ({ id: 7 })),
  actOnRun: vi.fn(async () => ({ id: 7 })),
}))

function renderForm() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={['/runs/new']}>
        <Routes>
          <Route path="/runs/new" element={<RunForm />} />
          <Route path="/runs/:runId" element={<p>The Run's page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

async function twoMembers() {
  renderForm()
  await waitFor(() => expect(screen.getByLabelText('Capital')).toHaveValue(500000))
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Trial' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add a strategy' }))
  fireEvent.click(screen.getByRole('button', { name: 'Add a strategy' }))
  fireEvent.change(screen.getByLabelText('Allocation 1'), { target: { value: '300000' } })
  fireEvent.change(screen.getByLabelText('Allocation kind 2'), { target: { value: 'percent' } })
  fireEvent.change(screen.getByLabelText('Allocation 2'), { target: { value: '40' } })
}

afterEach(() => vi.clearAllMocks())

// The whole form renders and changes field by field: give it room beside other files.
describe('RunForm', { timeout: 15_000 }, () => {
  it('adds up the allocations and shows day two by every policy', async () => {
    await twoMembers()
    expect(screen.getByLabelText('Strategy 1')).toHaveValue('alpha')
    expect(screen.getByLabelText('Strategy 2')).toHaveValue('res')
    expect(screen.getByText(/of ₹5,00,000.00 · 100%/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Day one result 1'), { target: { value: '10000' } })
    fireEvent.change(screen.getByLabelText('Day one result 2'), { target: { value: '-20000' } })
    const own = screen.getByRole('row', { name: /Own result \(chosen\)/ })
    expect(
      within(own)
        .getAllByRole('cell')
        .map((c) => c.textContent)
    ).toEqual(['Own result (chosen)', '₹3,10,000.00', '₹1,80,000.00'])
    const pooled = screen.getByRole('row', { name: /Pooled/ })
    expect(
      within(pooled)
        .getAllByRole('cell')
        .slice(1)
        .map((c) => c.textContent)
    ).toEqual(['₹2,94,000.00', '₹1,96,000.00'])
  })

  it('cannot choose a strategy a Run does not take, and says why', async () => {
    renderForm()
    fireEvent.click(await screen.findByRole('button', { name: 'Add a strategy' }))
    const gold = await screen.findByRole('option', { name: /Gold/ })
    expect(gold).toBeDisabled()
    expect(gold).toHaveTextContent('Runs take NSE and BSE strategies for now')
  })

  it('a shared Run cannot keep each strategy its own result', async () => {
    renderForm()
    fireEvent.click(await screen.findByRole('radio', { name: /^Shared/ }))
    expect(screen.getByRole('radio', { name: /^Own result/ })).toBeDisabled()
    expect(screen.getByRole('radio', { name: /^Pooled/ })).toBeChecked()
  })

  it('saves the definition and starts it', async () => {
    await twoMembers()
    fireEvent.click(screen.getByRole('button', { name: 'Save and start' }))
    expect(await screen.findByText("The Run's page")).toBeInTheDocument()
    expect(vi.mocked(createRun).mock.calls[0][0]).toEqual({
      name: 'Trial',
      capital: 500000,
      mode: 'isolated',
      policy: 'own',
      limit: 'capital',
      loss_limit_pct: null,
      members: [
        {
          script_id: 'alpha',
          label: undefined,
          allocation_kind: 'fixed',
          allocation_value: 300000,
          sizing: 'platform',
        },
        {
          script_id: 'res',
          label: undefined,
          allocation_kind: 'percent',
          allocation_value: 40,
          sizing: 'platform',
        },
      ],
    })
    expect(actOnRun).toHaveBeenCalledWith(7, 'start')
  })
})

describe('dayTwo', () => {
  it('opens day two as portfolio/run_capital.day_open does', () => {
    // The worked example in docs/design/63-runs.
    expect(dayTwo([300000, 200000], [10000, -20000])).toEqual({
      reset: [300000, 200000],
      own: [310000, 180000],
      pooled: [294000, 196000],
    })
  })
})
