import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@/test/test-utils'
import { HostedScriptsPanel } from './HostedScriptsPanel'

const api = vi.hoisted(() => ({
  getStrategies: vi.fn(),
  startStrategy: vi.fn(),
  stopStrategy: vi.fn(),
  getHostedScripts: vi.fn(),
  getOrderTags: vi.fn(),
  followScript: vi.fn(),
}))
vi.mock('@/api/python-strategy', () => ({
  pythonStrategyApi: {
    getStrategies: api.getStrategies,
    startStrategy: api.startStrategy,
    stopStrategy: api.stopStrategy,
  },
}))
vi.mock('@/api/research', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/research')>()),
  getHostedScripts: api.getHostedScripts,
  getOrderTags: api.getOrderTags,
  followScript: api.followScript,
}))

const script = (overrides = {}) => ({
  id: 'ema_cross_1',
  name: 'EMA cross',
  file_name: 'ema_cross_1.py',
  exchange: 'NSE',
  status: 'scheduled',
  process_id: null,
  last_started: null,
  last_stopped: null,
  error_message: null,
  is_scheduled: true,
  schedule_start_time: '09:00',
  schedule_stop_time: '15:30',
  schedule_days: ['mon', 'tue'],
  created_at: '',
  updated_at: '',
  ...overrides,
})

function show() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <HostedScriptsPanel />
    </QueryClientProvider>
  )
}

describe('HostedScriptsPanel', () => {
  beforeEach(() => vi.clearAllMocks())

  it('starts and stops through the strategy host, as /python does', async () => {
    api.getStrategies.mockResolvedValue([script({ status: 'running' })])
    api.getHostedScripts.mockResolvedValue({})
    api.stopStrategy.mockResolvedValue({ status: 'success', message: 'Stopped' })
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(api.stopStrategy).toHaveBeenCalledWith('ema_cross_1'))
    expect(api.startStrategy).not.toHaveBeenCalled()
  })

  it('shows what Research recorded for a followed script', async () => {
    api.getStrategies.mockResolvedValue([script()])
    api.getHostedScripts.mockResolvedValue({
      ema_cross_1: {
        strategy_id: 4,
        key: 'ema_cross_1',
        order_tag: 'EMA Crossover Python',
        trades_today: 2,
        last_recorded: { run_id: 9, trading_date: '2026-10-09', net_pnl: 1250, trades: 1 },
      },
    })
    show()
    expect(
      await screen.findByText(/Orders tagged "EMA Crossover Python", 2 trades today/)
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /2026-10-09/ })).toHaveAttribute(
      'href',
      '/research/runs/9'
    )
    expect(screen.getByRole('button', { name: 'Cancel start' })).toBeInTheDocument()
  })

  it("adds a script with the tag its orders carry and that tag's symbols", async () => {
    api.getStrategies.mockResolvedValue([script({ status: 'stopped' })])
    api.getHostedScripts.mockResolvedValue({})
    api.getOrderTags.mockResolvedValue([
      { tag: 'EMA Crossover Python', orders: 4, last_order_at: null, symbols: ['NHPC', 'SBIN'] },
    ])
    api.followScript.mockResolvedValue({ strategy_id: 4, key: 'ema_cross_1', order_tag: 'x' })
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Add to Research' }))
    fireEvent.click(await screen.findByRole('button', { name: 'EMA Crossover Python (4)' }))
    fireEvent.click(
      screen.getAllByRole('button', { name: 'Add to Research' }).at(-1) as HTMLElement
    )
    await waitFor(() =>
      expect(api.followScript).toHaveBeenCalledWith('ema_cross_1', {
        order_tag: 'EMA Crossover Python',
        capital: 500000,
        universe: ['NHPC', 'SBIN'],
      })
    )
    expect(document.body.textContent).not.toMatch(/paper|\barm(ed|ing)?\b/i)
  })
})
