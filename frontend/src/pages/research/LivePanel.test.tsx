import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import type { LiveOverview } from '@/api/research'
import { fireEvent, render, screen } from '@/test/test-utils'
import { LivePanel } from './LivePanel'

const overview = vi.hoisted(() => ({
  current: {
    enabled: true,
    allotted: 150_000,
    kill_switch: { engaged: false, reason: null, by: null, at: null },
    sessions: [],
  } as LiveOverview,
}))

vi.mock('@/api/research', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/research')>()
  return { ...actual, getLiveOverview: vi.fn(async () => overview.current) }
})

function show() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <LivePanel />
    </QueryClientProvider>
  )
}

describe('LivePanel', () => {
  it('says how to turn Live on when the server has it off', async () => {
    overview.current = { ...overview.current, enabled: false }
    show()
    expect(await screen.findByText('Off on this server')).toBeInTheDocument()
    expect(screen.getByText(/set RESEARCH_LIVE_ENABLED = 'True' in .env/)).toBeInTheDocument()
  })

  it('asks before the kill switch closes every live position, in plain words', async () => {
    overview.current = { ...overview.current, enabled: true }
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Kill switch' }))
    expect(
      screen.getByText(/Every live session closes its positions and ends now/)
    ).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/paper|\barm(ed|ing)?\b/i)
  })

  it('offers to release a kill switch that is on, and shows why it was engaged', async () => {
    overview.current = {
      ...overview.current,
      kill_switch: { engaged: true, reason: 'broker outage', by: 'u', at: null },
    }
    show()
    expect(await screen.findByRole('button', { name: 'Release kill switch' })).toBeInTheDocument()
    expect(screen.getByText(/Reason: broker outage/)).toBeInTheDocument()
  })
})
