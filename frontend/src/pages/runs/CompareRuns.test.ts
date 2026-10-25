import { describe, expect, it } from 'vitest'
import type { PnlSeries } from '@/api/runs'
import { aligned, commonDays } from './CompareRuns'

const series = (key: string, opening: number, nets: [string, number][]): PnlSeries =>
  ({
    key,
    name: key,
    kind: 'run',
    summary: {} as PnlSeries['summary'],
    daily: nets.map(([date, net]) => ({
      date,
      opening,
      transfer: 0,
      pnl: net,
      charges: 10,
      net,
      trades: 2,
      cumulative: 0,
    })),
  }) as PnlSeries

describe('comparing Runs', () => {
  it('lines Runs up on the days they all traded', () => {
    const a = series('a', 500000, [
      ['2026-10-13', 1],
      ['2026-10-14', 1],
      ['2026-10-15', 1],
    ])
    const b = series('b', 100000, [
      ['2026-10-14', 1],
      ['2026-10-15', 1],
      ['2026-10-16', 1],
    ])
    expect(commonDays([a, b])).toEqual(['2026-10-14', '2026-10-15'])
  })

  it('measures each as a share of what it opened with, its fall from its best too', () => {
    const run = series('a', 100000, [
      ['2026-10-14', 2000],
      ['2026-10-15', -3060],
      ['2026-10-16', 500],
    ])
    const f = aligned(run, ['2026-10-14', '2026-10-15', '2026-10-16'])
    expect(f.returns.map((p) => Number(p.value.toFixed(10)))).toEqual([2, -1.06, -0.56])
    // From 1,02,000 down to 98,940: 3% below its best.
    expect(f.drawdownPct).toBeCloseTo(-3, 10)
    expect([f.net, f.won, f.days, f.best, f.worst, f.charges, f.trades]).toEqual([
      -560, 2, 3, 2000, -3060, 30, 6,
    ])
  })
})
