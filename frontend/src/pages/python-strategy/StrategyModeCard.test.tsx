import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { pythonStrategyApi } from '@/api/python-strategy'
import type { PythonStrategy } from '@/types/python-strategy'
import { StrategyModeCard } from './StrategyModeCard'

vi.mock('@/api/python-strategy', () => ({
  pythonStrategyApi: {
    setMode: vi.fn().mockResolvedValue({ status: 'success', message: 'Set to Sandbox mode.' }),
  },
}))

const strategy = (overrides: Partial<PythonStrategy>) =>
  ({
    id: 'alpha_1',
    name: 'Alpha',
    status: 'stopped',
    mode: 'live',
    ...overrides,
  }) as PythonStrategy

describe('StrategyModeCard', () => {
  it('asks before changing where the orders go, then saves the new mode', async () => {
    const onChanged = vi.fn()
    render(<StrategyModeCard strategy={strategy({})} onChanged={onChanged} />)

    expect(screen.getByRole('radio', { name: 'Live' })).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(screen.getByRole('radio', { name: 'Sandbox' }))
    expect(await screen.findByText('Keep this strategy in the Sandbox?')).toBeInTheDocument()
    expect(pythonStrategyApi.setMode).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Switch to Sandbox' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith('sandbox'))
    expect(pythonStrategyApi.setMode).toHaveBeenCalledWith('alpha_1', 'sandbox')
  })

  it('cannot be changed while the strategy runs', () => {
    render(
      <StrategyModeCard
        strategy={strategy({ status: 'running', mode: 'sandbox' })}
        onChanged={vi.fn()}
      />
    )

    expect(screen.getByRole('radio', { name: 'Live' })).toBeDisabled()
    expect(screen.getByText(/Stop the strategy to change this/)).toBeInTheDocument()
    expect(screen.getByText(/whether Analyzer mode is on or off/)).toBeInTheDocument()
  })
})
