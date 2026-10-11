import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@/test/test-utils'
import { KIND_LABEL, Reconciliation, RunLedger } from './RunLedger'

const ENTRY = {
  seq: 7,
  kind: 'FILL',
  timestamp: 1704080700,
  at: null,
  session_id: 41,
  experiment_id: 1,
  scenario_id: 2,
  strategy_instance_id: 0,
  strategy_id: 3,
  strategy_version_id: 4,
  config_version_id: 5,
  signal_id: null,
  platform_order_id: 'research:41:6',
  execution_order_id: 'SB1',
  fill_id: 7,
  trade_id: null,
  symbol: 'RELIANCE',
  side: 'BUY',
  quantity: 40,
  requested_price: 2498,
  fill_price: 2499.5,
  fees: 12.5,
  slippage: 0,
  gross_pnl: null,
  net_pnl: null,
  detail: 'signal',
}

vi.mock('@/api/research', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/research')>()
  return {
    ...actual,
    getRunLedger: vi.fn(async () => ({ items: [ENTRY], next_cursor: null })),
    getRunReconciliation: vi.fn(async () => ({
      passed: false,
      checks: [
        {
          name: 'Every order equals the book fills it was netted from',
          passed: true,
          detail: '',
          problems: [],
        },
        {
          name: "Every order's costs are allocated to its books in full",
          passed: false,
          detail: '1 found',
          problems: ['order 6: charged 12.5, allocated 10.0'],
        },
        {
          name: 'Every order reached a final state',
          passed: null,
          detail: '1 order(s) still open while the session runs.',
          problems: [],
        },
      ],
    })),
  }
})

function show(node: React.ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{node}</QueryClientProvider>)
}

describe('RunLedger', () => {
  it('shows each entry with its strategy, order and venue reference', async () => {
    show(<RunLedger runId={41} labels={new Map([[0, 'alpha (config v1)']])} />)
    expect(await screen.findByText('RELIANCE')).toBeInTheDocument()
    expect(screen.getAllByText('alpha (config v1)').length).toBeGreaterThan(0)
    expect(screen.getByText('6 (SB1)')).toBeInTheDocument()
    expect(screen.getByText('2499.50')).toBeInTheDocument()
  })

  it('names every kind in the platform words, never paper or arm', () => {
    const text = Object.values(KIND_LABEL).join(' ')
    expect(text).not.toMatch(/paper/i)
    expect(text).not.toMatch(/\barm(ed|ing)?\b/i)
  })
})

describe('Reconciliation', () => {
  it('says which checks passed, which failed and which wait for the run', async () => {
    show(<Reconciliation runId={41} live={false} />)
    expect(await screen.findByText('Failed')).toBeInTheDocument()
    expect(screen.getByText('Passed')).toBeInTheDocument()
    expect(screen.getByText('Not yet')).toBeInTheDocument()
    expect(screen.getByText('order 6: charged 12.5, allocated 10.0')).toBeInTheDocument()
  })
})
