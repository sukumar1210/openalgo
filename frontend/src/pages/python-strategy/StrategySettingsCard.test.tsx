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

const NAMES = [
  'ALGO_PAPER_DISASTER_STOP_PCT',
  'ALGO_PAPER_EXIT_TIME',
  'ALGO_PAPER_MODEL',
  'ALGO_PAPER_RUN',
  'ALGO_SHADOW',
]

describe('StrategySettingsCard', () => {
  it('offers exactly the names the server lists, and saves the filled ones', async () => {
    const saved = { ALGO_PAPER_EXIT_TIME: '15:10', ALGO_SHADOW: '0' }
    vi.mocked(pythonStrategyApi.setSettings).mockResolvedValueOnce(saved)
    const onChanged = vi.fn()
    render(
      <StrategySettingsCard strategy={strategy({ setting_names: NAMES })} onChanged={onChanged} />
    )

    expect(screen.getAllByRole('textbox')).toHaveLength(NAMES.length)
    expect(screen.getByLabelText('ALGO_PAPER_EXIT_TIME')).toHaveValue('15:10')
    fireEvent.change(screen.getByLabelText('ALGO_SHADOW'), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(saved))
    expect(pythonStrategyApi.setSettings).toHaveBeenCalledWith('alpha', saved)
  })

  it('shows a saved name that is no longer allowed, and drops it on save', async () => {
    vi.mocked(pythonStrategyApi.setSettings).mockResolvedValueOnce({})
    render(
      <StrategySettingsCard
        strategy={strategy({ setting_names: NAMES, env: { ALGO_DATA_DIR: '/tmp' } })}
        onChanged={vi.fn()}
      />
    )
    expect(screen.getByText(/ALGO_DATA_DIR is not a setting/)).toBeInTheDocument()
    expect(screen.queryByLabelText('ALGO_DATA_DIR')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(pythonStrategyApi.setSettings).toHaveBeenCalledWith('alpha', {}))
  })
})
