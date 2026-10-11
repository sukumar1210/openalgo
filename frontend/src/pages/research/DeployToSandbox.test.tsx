import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import type { Scenario } from '@/api/research'
import { render, screen } from '@/test/test-utils'
import { DeployToSandbox } from './DeployToSandbox'

const SCENARIO = {
  id: 7,
  experiment_id: 1,
  name: 'S2 shared',
  mode: 'SHARED',
  dataset_spec: { symbols: ['AAA'], exchange: 'NSE', interval: '5m', start: '', end: '' },
  dataset_id: null,
  cost_model: { preset: 'india_intraday_nse', slippage_bps: 2 },
  execution_model: { fill: 'next_open' },
  risk_policy: { max_gross_leverage: 1 },
  shared_capital: null,
  created_at: null,
  instances: [],
} as Scenario

function show() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <DeployToSandbox scenario={SCENARIO} open onOpenChange={() => {}} />
    </QueryClientProvider>
  )
}

describe('DeployToSandbox', () => {
  it('says before anything starts that broker orders are off (PRD section 32)', () => {
    show()
    expect(screen.getByText('Sandbox')).toBeInTheDocument()
    expect(screen.getByText(/Off\. Orders go to the sandbox only\./)).toBeInTheDocument()
  })

  it('uses the platform words, never paper or arm', () => {
    show()
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/paper/i)
    expect(text).not.toMatch(/\barm(ed|ing)?\b/i)
  })

  it('defaults to weekdays and a schedule inside market hours', () => {
    show()
    expect(screen.getByLabelText('Start (IST)')).toHaveValue('09:10')
    expect(screen.getByLabelText('Stop (IST)')).toHaveValue('15:25')
    for (const day of ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']) {
      expect(screen.getByLabelText(day)).toBeChecked()
    }
    expect(screen.getByLabelText('Sat')).not.toBeChecked()
  })
})

describe('DeployToSandbox environments', () => {
  it('names Shadow as sending nothing anywhere', async () => {
    const { fireEvent } = await import('@/test/test-utils')
    show()
    fireEvent.click(screen.getByRole('combobox', { name: 'Where orders go' }))
    fireEvent.click(await screen.findByText('Shadow: filled inside the session, nothing sent'))
    expect(screen.getByText(/Nothing is sent anywhere/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Deploy in Shadow' })).toBeEnabled()
  })
})
