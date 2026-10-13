import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { pythonStrategyApi } from '@/api/python-strategy'
import type { PythonStrategy, SymbolList } from '@/types/python-strategy'
import { StrategySymbolsCard, SymbolListsDialog } from './SymbolLists'

const core: SymbolList = {
  id: 'core',
  name: 'Alpha universe',
  exchange: 'NSE',
  symbols: ['RELIANCE', 'TCS'],
  checked: true,
  updated_at: '',
  used_by: ['Alpha a adapter'],
}
const metals: SymbolList = { ...core, id: 'mcx', name: 'Metals', exchange: 'MCX', used_by: [] }

vi.mock('@/api/python-strategy', () => ({
  pythonStrategyApi: {
    getExchanges: vi.fn().mockResolvedValue([]),
    listSymbolLists: vi.fn(),
    saveSymbolList: vi.fn(),
    deleteSymbolList: vi.fn(),
    setSymbolList: vi.fn().mockResolvedValue(undefined),
  },
}))

function wrap(node: ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{node}</QueryClientProvider>)
}

beforeEach(() => {
  vi.mocked(pythonStrategyApi.listSymbolLists).mockResolvedValue([core, metals])
})

describe('SymbolListsDialog', () => {
  it('saves pasted symbols as a list and shows why the server refused one', async () => {
    vi.mocked(pythonStrategyApi.saveSymbolList).mockRejectedValueOnce({
      response: { data: { message: 'Not found on NSE: TATAMOTOR.' } },
    })
    wrap(<SymbolListsDialog />)
    fireEvent.click(screen.getByRole('button', { name: 'Symbol lists' }))
    expect(await screen.findByText('Used by Alpha a adapter')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'New list' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Banks' } })
    fireEvent.change(screen.getByLabelText('Symbols'), { target: { value: 'SBIN TATAMOTOR' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save list' }))

    expect(await screen.findByText('Not found on NSE: TATAMOTOR.')).toBeInTheDocument()
    expect(pythonStrategyApi.saveSymbolList).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'Banks', exchange: 'NSE', symbols: 'SBIN TATAMOTOR' })
    )
  })

  it('will not delete a list a strategy uses', async () => {
    wrap(<SymbolListsDialog />)
    fireEvent.click(screen.getByRole('button', { name: 'Symbol lists' }))
    await screen.findByText('Used by Alpha a adapter')

    const [coreDelete, metalsDelete] = screen.getAllByRole('button', { name: 'Delete' })
    expect(coreDelete).toBeDisabled()
    expect(metalsDelete).toBeEnabled()
  })
})

describe('StrategySymbolsCard', () => {
  const strategy = (overrides: Partial<PythonStrategy>) =>
    ({
      id: 'alpha_1',
      exchange: 'NSE',
      status: 'stopped',
      symbol_list: null,
      ...overrides,
    }) as PythonStrategy

  it('offers the lists of its own exchange and saves the choice', async () => {
    const onChanged = vi.fn()
    wrap(<StrategySymbolsCard strategy={strategy({})} onChanged={onChanged} />)
    await screen.findByRole('option', { name: 'Alpha universe (2 symbols)' })
    expect(screen.queryByRole('option', { name: /Metals/ })).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Symbol list'), { target: { value: 'core' } })

    await waitFor(() =>
      expect(onChanged).toHaveBeenCalledWith({ id: 'core', name: 'Alpha universe', count: 2 })
    )
    expect(pythonStrategyApi.setSymbolList).toHaveBeenCalledWith('alpha_1', 'core')
  })

  it('cannot be changed while the strategy runs', () => {
    wrap(<StrategySymbolsCard strategy={strategy({ status: 'running' })} onChanged={vi.fn()} />)

    expect(screen.getByLabelText('Symbol list')).toBeDisabled()
  })
})
