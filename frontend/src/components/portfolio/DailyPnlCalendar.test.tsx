import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { DailyPnlCalendar, dayColor } from './DailyPnlCalendar'

const format = (v: number) => `Rs ${v.toFixed(0)}`
const days = [
  { date: '2026-09-30', net: 200, opening: 100000, charges: 12, trades: 4 },
  { date: '2026-10-14', net: 1000, opening: 100000, charges: 20, trades: 6 },
  { date: '2026-10-15', net: -500, opening: 100000, charges: 15, trades: 2 },
]

describe('DailyPnlCalendar', () => {
  it('opens on the last day given, a gain green and a loss red', () => {
    render(<DailyPnlCalendar days={days} format={format} />)
    expect(screen.getByRole('heading', { name: 'October 2026' })).toBeInTheDocument()
    const gain = screen.getByRole('button', { name: /2026-10-14: net Rs 1000/ })
    const loss = screen.getByRole('button', { name: /2026-10-15: net Rs -500/ })
    expect(gain.getAttribute('style')).toContain('34, 197, 94')
    expect(loss.getAttribute('style')).toContain('239, 68, 68')
    expect(gain).toHaveAccessibleName(/1.00% of opening capital, charges Rs 20, 6 trades/)
  })

  it('scales to the largest day given, so a smaller day is paler', () => {
    const strong = dayColor(1000, 1000)
    const pale = dayColor(200, 1000)
    const alpha = (c: string) => Number(c.slice(c.lastIndexOf(',') + 1, -1))
    expect(alpha(strong)).toBeGreaterThan(alpha(pale))
    expect(dayColor(0, 1000)).toBe('transparent')
  })

  it('hands a clicked day to the page and marks it chosen', () => {
    const onSelect = vi.fn()
    const { rerender } = render(
      <DailyPnlCalendar days={days} format={format} onSelect={onSelect} />
    )
    fireEvent.click(screen.getByRole('button', { name: /2026-10-15/ }))
    expect(onSelect).toHaveBeenCalledWith('2026-10-15')
    rerender(
      <DailyPnlCalendar days={days} format={format} onSelect={onSelect} selected="2026-10-15" />
    )
    expect(screen.getByRole('button', { name: /2026-10-15/ })).toHaveAttribute(
      'aria-pressed',
      'true'
    )
  })

  it('totals the month and the year beside the grid, and moves between months', () => {
    render(<DailyPnlCalendar days={days} format={format} />)
    expect(screen.getByText('Month').nextSibling).toHaveTextContent('Rs 500')
    expect(screen.getByText('Year').nextSibling).toHaveTextContent('Rs 700')
    fireEvent.click(screen.getByRole('button', { name: 'Previous month' }))
    expect(screen.getByRole('heading', { name: 'September 2026' })).toBeInTheDocument()
    expect(screen.getByText('Month').nextSibling).toHaveTextContent('Rs 200')
  })
})
