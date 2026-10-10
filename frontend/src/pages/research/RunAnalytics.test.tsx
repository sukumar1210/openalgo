import { describe, expect, it } from 'vitest'
import type { TogetherMetrics } from '@/api/research'
import { render, screen } from '@/test/test-utils'
import { TogetherCard } from './RunAnalytics'

const TOGETHER: TogetherMetrics = {
  order_groups: 40,
  conflict_rate_pct: 12.5,
  netting: { orders_avoided: 6, notional_netted: 300000, costs_saved: 812.4 },
  overlap: [{ a: 0, b: 1, same_pct: 20, opposite_pct: 5 }],
  correlation: [{ a: 0, b: 1, value: -0.31 }],
  reductions: 2,
}
const LABELS = new Map([
  [0, 'alpha (config v1)'],
  [1, 'beta (config v3)'],
])

describe('TogetherCard', () => {
  it('shows what sharing a portfolio saved and how alike the strategies traded', () => {
    render(<TogetherCard together={TOGETHER} labels={LABELS} shared />)
    expect(screen.getByText('Orders avoided')).toBeInTheDocument()
    expect(screen.getByText('6')).toBeInTheDocument()
    expect(screen.getByText('alpha (config v1) and beta (config v3)')).toBeInTheDocument()
    expect(screen.getByText('-0.31')).toBeInTheDocument()
  })

  it('does not claim netting for isolated strategies', () => {
    render(<TogetherCard together={TOGETHER} labels={LABELS} shared={false} />)
    expect(screen.queryByText('Orders avoided')).not.toBeInTheDocument()
    expect(screen.getByText(/nothing is netted or shared/)).toBeInTheDocument()
  })

  it('says a limit stopped the day in a sentence, for the strategy it stopped', () => {
    render(
      <TogetherCard
        labels={LABELS}
        shared={false}
        breaches={[
          { portfolio: 1, ts: 1704081600, reason: 'daily_loss', day_pnl: -4000, detail: '' },
        ]}
      />
    )
    expect(screen.getByText(/Daily loss limit reached for beta \(config v3\)/)).toBeInTheDocument()
  })
})
