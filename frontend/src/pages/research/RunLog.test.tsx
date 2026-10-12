import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@/test/test-utils'
import { RunLog } from './RunLog'

const getRunLog = vi.hoisted(() => vi.fn())
vi.mock('@/api/research', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/research')>()),
  getRunLog,
}))
vi.mock('@/components/ui/log-viewer', () => ({
  LogViewer: ({ value }: { value: string }) => <pre>{value}</pre>,
}))

function show(live = false) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <RunLog runId={3} live={live} />
    </QueryClientProvider>
  )
}

describe('RunLog', () => {
  it("shows the run's log, including a strategy's traceback", async () => {
    getRunLog.mockResolvedValue({
      content: 'ERROR in sandbox_worker: a strategy failed\nValueError: cum_delta',
      note: null,
      size_bytes: 60,
      truncated: false,
      updated_at: null,
    })
    show()
    expect(await screen.findByText(/ValueError: cum_delta/)).toBeInTheDocument()
    expect(getRunLog).toHaveBeenCalledWith(3)
  })

  it('says why there is no log instead of showing an empty box', async () => {
    getRunLog.mockResolvedValue({
      content: '',
      note: 'Nothing yet: the log starts when the run does.',
      size_bytes: 0,
      truncated: false,
      updated_at: null,
    })
    show(true)
    expect(await screen.findByText(/the log starts when the run does/)).toBeInTheDocument()
    expect(screen.getByText(/refreshed every 5 seconds/)).toBeInTheDocument()
  })
})
