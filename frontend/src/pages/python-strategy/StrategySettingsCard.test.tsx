import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { pythonStrategyApi } from '@/api/python-strategy'
import type { PythonStrategy } from '@/types/python-strategy'
import { StrategySettingsCard } from './StrategySettingsCard'

vi.mock('@/api/python-strategy', () => ({
  pythonStrategyApi: { setSettings: vi.fn() },
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
    env: { ALGO_PAPER_EXIT_TIME: '15:10' },
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

describe('StrategySettingsCard', () => {
  it('saves the settings as names and values', async () => {
    const saved = { ALGO_PAPER_EXIT_TIME: '15:10', ALGO_PAPER_RUN: 'alpha_a_1510' }
    vi.mocked(pythonStrategyApi.setSettings).mockResolvedValueOnce(saved)
    const onChanged = vi.fn()
    render(<StrategySettingsCard strategy={strategy({})} onChanged={onChanged} />)

    expect(screen.getByDisplayValue('ALGO_PAPER_EXIT_TIME')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Add setting' }))
    fireEvent.change(screen.getByLabelText('Setting 2 name'), {
      target: { value: 'algo_paper_run' },
    })
    fireEvent.change(screen.getByLabelText('Setting 2 value'), {
      target: { value: 'alpha_a_1510' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(saved))
    expect(pythonStrategyApi.setSettings).toHaveBeenCalledWith('alpha', saved)
  })

  it('refuses a name a strategy cannot be given', () => {
    render(<StrategySettingsCard strategy={strategy({ env: {} })} onChanged={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add setting' }))
    fireEvent.change(screen.getByLabelText('Setting 1 name'), { target: { value: 'PYTHONPATH' } })
    expect(screen.getByText(/PYTHONPATH is not a setting/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })
})
