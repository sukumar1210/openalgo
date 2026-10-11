import { describe, expect, it } from 'vitest'
import { render, screen } from '@/test/test-utils'
import { ReplayNotice } from './RunDetail'

describe('ReplayNotice', () => {
  it('names each day a replayed script did not finish, and what it was refused', () => {
    render(
      <ReplayNotice
        replay={{
          days: 20,
          stopped_early: [
            { day: '2025-10-09', at: '10:00', message: 'The script stopped with an error: x' },
          ],
          unsupported: { optionchain: 3 },
        }}
      />
    )
    expect(screen.getByText('The script did not finish 1 of its 20 days')).toBeInTheDocument()
    expect(screen.getByText(/2025-10-09 at 10:00/)).toBeInTheDocument()
    expect(screen.getByText(/optionchain \(3 times\)/)).toBeInTheDocument()
  })

  it('says nothing when every day ended cleanly', () => {
    const { container } = render(<ReplayNotice replay={{ days: 5, stopped_early: [] }} />)
    expect(container.textContent).toBe('')
  })
})
