import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { pythonStrategyApi } from '@/api/python-strategy'
import type { PythonStrategy } from '@/types/python-strategy'
import { StrategyCapitalCard } from './StrategyCapitalCard'

vi.mock('@/api/python-strategy', () => ({
  pythonStrategyApi: { setCapital: vi.fn() },
}))

function strategy(overrides: Partial<PythonStrategy>): PythonStrategy {
  return {
    id: 'alpha',
    name: 'Alpha',
    file_name: 'alpha.py',
    exchange: 'NSE',
    mode: 'sandbox',
    symbol_list: null,
    capital: 500000,
    capital_today: 512345.5,
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

describe('StrategyCapitalCard', () => {
  it('shows what a run starting today is handed and saves a new starting capital', async () => {
    vi.mocked(pythonStrategyApi.setCapital).mockResolvedValueOnce(300000)
    const onChanged = vi.fn()
    render(<StrategyCapitalCard strategy={strategy({})} onChanged={onChanged} />)

    expect(screen.getByText(/gets ₹5,12,345\.50/)).toBeInTheDocument()
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Starting capital'), { target: { value: '300000' } })
    fireEvent.click(save)

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(300000))
    expect(pythonStrategyApi.setCapital).toHaveBeenCalledWith('alpha', 300000)
  })

  it('cannot be changed while the strategy runs', () => {
    render(<StrategyCapitalCard strategy={strategy({ status: 'running' })} onChanged={vi.fn()} />)
    expect(screen.getByLabelText('Starting capital')).toBeDisabled()
    expect(screen.getByText(/Stop the strategy to change this/)).toBeInTheDocument()
  })

  it('says so when today’s capital could not be worked out', () => {
    render(<StrategyCapitalCard strategy={strategy({ capital_today: null })} onChanged={vi.fn()} />)
    expect(screen.getByText(/could not be worked out right now/)).toBeInTheDocument()
  })
})
