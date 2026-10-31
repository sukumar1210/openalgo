import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { pythonStrategyApi } from '@/api/python-strategy'
import type { PythonStrategy } from '@/types/python-strategy'
import { StrategyAccountCard } from './StrategyAccountCard'

vi.mock('@/api/python-strategy', () => ({
  pythonStrategyApi: { bindAccount: vi.fn(), fundAccount: vi.fn(), closeAccount: vi.fn() },
}))

function strategy(overrides: Partial<PythonStrategy>): PythonStrategy {
  return {
    id: 'alpha',
    name: 'Alpha',
    file_name: 'alpha.py',
    exchange: 'NSE',
    mode: 'live',
    symbol_list: null,
    capital: 500000,
    account: null,
    status: 'stopped',
    process_id: null,
    last_started: null,
    last_stopped: null,
    error_message: null,
    is_scheduled: false,
    schedule_start_time: null,
    schedule_stop_time: null,
    schedule_days: [],
    created_at: '',
    updated_at: '',
    ...overrides,
  }
}

const ACCOUNT = {
  token: '0123abcd',
  label: 'alpha_a_1510',
  status: 'active' as const,
  starting_capital: 500000,
  equity: 500000,
}

describe('StrategyAccountCard', () => {
  it('opens an account of its own with the name and capital given', async () => {
    vi.mocked(pythonStrategyApi.bindAccount).mockResolvedValueOnce(ACCOUNT)
    const onChanged = vi.fn()
    render(<StrategyAccountCard strategy={strategy({})} onChanged={onChanged} />)

    expect(screen.getByText(/It trades the main Sandbox/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'alpha_a_1510' } })
    fireEvent.click(screen.getByRole('button', { name: 'Open account' }))

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(ACCOUNT))
    expect(pythonStrategyApi.bindAccount).toHaveBeenCalledWith('alpha', 'alpha_a_1510', 500000)
  })

  it('shows its account and moves money in and out of it', async () => {
    vi.mocked(pythonStrategyApi.fundAccount).mockResolvedValueOnce({ ...ACCOUNT, equity: 400000 })
    const onChanged = vi.fn()
    render(<StrategyAccountCard strategy={strategy({ account: ACCOUNT })} onChanged={onChanged} />)

    expect(screen.getByText('alpha_a_1510')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '100000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Take out' }))

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ ...ACCOUNT, equity: 400000 }))
    expect(pythonStrategyApi.fundAccount).toHaveBeenCalledWith('alpha', -100000)
  })

  it('cannot be changed while the strategy runs', () => {
    render(
      <StrategyAccountCard
        strategy={strategy({ account: ACCOUNT, status: 'running' })}
        onChanged={vi.fn()}
      />
    )
    expect(screen.getByLabelText('Amount')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Close account' })).toBeDisabled()
    expect(screen.getByText('Stop the strategy to change this.')).toBeInTheDocument()
  })
})
