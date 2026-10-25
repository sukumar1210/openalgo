import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { getPnl, type PnlQuery, type PnlSeries, type Run } from '@/api/runs'
import PnlDashboard, { windowOf } from './PnlDashboard'
import { dayRows, toCsv } from './PnlReport'

vi.mock('@/components/portfolio/PortfolioLineChart', () => ({
  PortfolioLineChart: ({ series }: { series: { name: string }[] }) => (
    <ul aria-label="chart">
      {series.map((s) => (
        <li key={s.name}>{s.name}</li>
      ))}
    </ul>
  ),
}))

const summary = {
  total_pnl: 0,
  gross_pnl: 0,
  charges: 0,
  unmodelled_fills: 0,
  return_pct: 0,
  days: 2,
  winning_days: 1,
  best_day: 0,
  worst_day: 0,
  max_drawdown: 0,
  max_drawdown_pct: 0,
  trades: 0,
  last_day: '2026-10-15',
}

function series(key: string, name: string, nets: [string, number][]): PnlSeries {
  let total = 0
  return {
    key,
    name,
    kind: 'run',
    summary: { ...summary, total_pnl: nets.reduce((t, [, n]) => t + n, 0) },
    daily: nets.map(([date, net]) => {
      total += net
      return {
        date,
        opening: 500000,
        transfer: 0,
        pnl: net + 20,
        charges: 20,
        net,
        trades: 2,
        cumulative: total,
      }
    }),
  }
}

const RUNS = [
  { id: 1, name: 'Alpha, isolated' },
  { id: 2, name: 'Alpha, pooled' },
].map(
  ({ id, name }) =>
    ({
      id,
      name,
      status: 'active',
      version: { members: [{ seat: `s${id}`, label: `Member ${id}` }] },
      days: [],
    }) as unknown as Run
)

vi.mock('@/api/runs', async (original) => ({
  ...(await original<typeof import('@/api/runs')>()),
  listRuns: vi.fn(async () => RUNS),
  getPnl: vi.fn(async (query: PnlQuery) => {
    const out: PnlSeries[] = []
    for (const id of query.runs ?? [])
      out.push(
        series(`run:${id}`, RUNS[id - 1].name, [
          ['2026-10-14', 1000 * id],
          ['2026-10-15', -400],
        ])
      )
    if (query.main) out.push(series('main', 'Main Sandbox account', [['2026-10-15', 50]]))
    const byDate = new Map<string, number>()
    for (const s of out)
      for (const d of s.daily) byDate.set(d.date, (byDate.get(d.date) ?? 0) + d.net)
    return {
      series: out,
      combined: series(
        'combined',
        'Combined',
        [...byDate.entries()].sort().map(([d, n]) => [d, n] as [string, number])
      ),
    }
  }),
}))

vi.mock('@/api/strategy-dashboard', async (original) => ({
  ...(await original<typeof import('@/api/strategy-dashboard')>()),
  getDashboard: vi.fn(async () => ({ capital: 1e7, strategies: [], combined: null })),
}))

function renderPage() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={['/runs/pnl']}>
        <PnlDashboard />
      </MemoryRouter>
    </QueryClientProvider>
  )
}

const lastQuery = () => vi.mocked(getPnl).mock.calls.at(-1)?.[0]

/** One CSV line's cells, a quoted cell's commas and doubled quotes kept. */
function csvCells(line: string): string[] {
  const out: string[] = []
  let cell = ''
  let quoted = false
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (quoted && ch === '"' && line[i + 1] === '"') {
      cell += '"'
      i++
    } else if (ch === '"') {
      quoted = !quoted
    } else if (ch === ',' && !quoted) {
      out.push(cell)
      cell = ''
    } else {
      cell += ch
    }
  }
  out.push(cell)
  return out
}

afterEach(() => vi.clearAllMocks())

describe('PnlDashboard', () => {
  it('shows every Run that traded, and the picker changes the series', async () => {
    renderPage()
    const chart = await screen.findAllByRole('list', { name: 'chart' })
    expect(within(chart[0]).getByText('Alpha, isolated')).toBeInTheDocument()
    expect(lastQuery()?.runs).toEqual([1, 2])

    fireEvent.click(screen.getByRole('button', { name: 'Main Sandbox account' }))
    await waitFor(() => expect(lastQuery()?.main).toBe(true))
    expect(
      within((await screen.findAllByRole('list', { name: 'chart' }))[0]).getByText(
        'Main Sandbox account'
      )
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Alpha, pooled' }))
    await waitFor(() => expect(lastQuery()?.runs).toEqual([1]))
    // One Run chosen: a strategy within it can be shown on its own.
    fireEvent.change(screen.getByRole('combobox', { name: 'Strategy within the Run' }), {
      target: { value: 's1' },
    })
    await waitFor(() => expect(lastQuery()?.members).toEqual([[1, 's1']]))
    expect(lastQuery()?.runs).toEqual([])
  })

  it('opens a day chosen in the calendar beside it', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /2026-10-15: net/ }))
    const aside = screen.getByRole('complementary', { name: 'The day chosen' })
    expect(within(aside).getByRole('heading', { name: '2026-10-15' })).toBeInTheDocument()
    expect(within(aside).getByText('Alpha, isolated')).toBeInTheDocument()
    expect(within(aside).getByText('Alpha, pooled')).toBeInTheDocument()
  })

  it('downloads exactly the rows the table shows', async () => {
    const blobs: Blob[] = []
    URL.createObjectURL = vi.fn((blob: Blob) => {
      blobs.push(blob)
      return 'blob:pnl'
    }) as typeof URL.createObjectURL
    URL.revokeObjectURL = vi.fn()
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Download CSV' }))
    // jsdom's Blob has no text(); a FileReader reads it as a browser would.
    const text = await new Promise<string>((resolve) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.readAsText(blobs[0])
    })
    const lines = text.split('\n')
    const table = screen.getByRole('table', { name: 'Every day' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(lines).toHaveLength(rows.length + 1)
    rows.forEach((row, i) => {
      const cells = within(row)
        .getAllByRole('cell')
        .map((c) => c.textContent)
      const [date, name, , , , , net, trades] = csvCells(lines[i + 1])
      expect([cells[0], cells[1], cells[7]]).toEqual([date, name, trades])
      expect(Number((cells[6] ?? '').replace(/[^0-9.-]/g, ''))).toBe(Number(net))
    })
  })
})

describe('the P&L helpers', () => {
  it('quotes a name with a comma in the CSV', () => {
    const csv = toCsv(dayRows([series('run:1', 'Alpha, isolated', [['2026-10-14', 10]])]))
    expect(csv.split('\n')[1]).toBe('2026-10-14,"Alpha, isolated",500000,0,30,20,10,2')
  })

  it('windows end today', () => {
    const today = new Date(2026, 9, 15) // a Thursday
    expect(windowOf('week', today)).toEqual({ from: '2026-10-12', to: '2026-10-15' })
    expect(windowOf('month', today)).toEqual({ from: '2026-10-01', to: '2026-10-15' })
    expect(windowOf('all', today)).toEqual({})
  })
})
