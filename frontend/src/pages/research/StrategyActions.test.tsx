import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import type { StrategyListItem } from '@/api/research'
import { fireEvent, render, screen } from '@/test/test-utils'
import { StrategyActions } from './StrategyActions'

function strategy(overrides: Partial<StrategyListItem>): StrategyListItem {
  return {
    id: 1,
    key: 'alpha',
    source: 'entry_point',
    created_at: null,
    latest_version: null,
    latest_config: { id: 3, version_no: 2 } as StrategyListItem['latest_config'],
    status: 'ready',
    active_deployments: 0,
    last_run: null,
    ...overrides,
  } as StrategyListItem
}

function show(item: StrategyListItem) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <StrategyActions strategy={item} />
    </QueryClientProvider>
  )
}

describe('StrategyActions', () => {
  it('offers backtest and deploy only for an installed, configured strategy', () => {
    show(strategy({ status: 'not_installed' }))
    expect(screen.getByRole('button', { name: 'Backtest' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Deploy' })).toBeDisabled()
  })

  it('pauses only what is trading, and says it acts on whole sessions', () => {
    show(strategy({ status: 'trading', active_deployments: 1 }))
    expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    expect(screen.getByText(/Other strategies in the same session stop too/)).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/paper|\barm(ed|ing)?\b/i)
  })

  it('has nothing to pause or stop when the strategy is not deployed', () => {
    show(strategy({}))
    expect(screen.getByRole('button', { name: 'Pause' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeDisabled()
  })
})
