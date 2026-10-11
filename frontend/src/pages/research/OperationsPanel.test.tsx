import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import type { Operations } from '@/api/research'
import { fireEvent, render, screen } from '@/test/test-utils'
import { OperationsPanel } from './OperationsPanel'

const limits = {
  backtests: 2,
  sessions: 4,
  memory_mb: 2048,
  cpu_seconds: 3600,
  job_timeout_seconds: 7200,
}
const overview = vi.hoisted(() => ({ current: null as unknown as Operations }))

vi.mock('@/api/research', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/research')>()
  return { ...actual, getOperations: vi.fn(async () => overview.current) }
})

function show() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <OperationsPanel />
    </QueryClientProvider>
  )
}

describe('OperationsPanel', () => {
  it('lists each worker with its memory and how long it has run', async () => {
    overview.current = {
      limits,
      workers: [
        {
          run_id: 7,
          kind: 'session',
          environment: 'LIVE',
          status: 'RUNNING',
          pid: 11,
          memory_mb: 212.5,
          running_seconds: 3720,
          heartbeat_at: '2026-10-12T04:01:02',
          lease_owner: 'host:11',
          restarts: 1,
        },
        {
          run_id: 9,
          kind: 'backtest',
          environment: 'BACKTEST',
          status: 'RUNNING',
          pid: 12,
          memory_mb: null,
          running_seconds: 30,
          heartbeat_at: null,
          lease_owner: null,
          restarts: 0,
        },
      ],
    }
    show()
    expect(await screen.findByText('Live session')).toBeInTheDocument()
    expect(screen.getByText('212.5 MB')).toBeInTheDocument()
    expect(screen.getByText('1 h 2 min')).toBeInTheDocument()
    expect(screen.getByText('Backtest')).toBeInTheDocument()
    expect(screen.getByText(/1 of 2 backtests and 1 of 4 sessions running/)).toBeInTheDocument()
  })

  it('asks before stopping every session, in plain words', async () => {
    overview.current = {
      limits,
      workers: [
        {
          run_id: 7,
          kind: 'session',
          environment: 'SANDBOX',
          status: 'RUNNING',
          pid: 11,
          memory_mb: 100,
          running_seconds: 60,
          heartbeat_at: null,
          lease_owner: null,
          restarts: 0,
        },
      ],
    }
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Stop all sessions' }))
    expect(screen.getByText(/closes its positions and finishes for today/)).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/paper|\barm(ed|ing)?\b/i)
  })

  it('offers no stop when no session is running', async () => {
    overview.current = { limits, workers: [] }
    show()
    expect(await screen.findByText('No worker is running.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop all sessions' })).toBeDisabled()
  })
})
