import { describe, expect, it } from 'vitest'
import { configFromForm } from './StrategyDetail'

const FORM = {
  capital: '500000',
  allocationPct: '60',
  leverage: '2',
  product: 'MIS' as const,
  entry_time: '09:16',
  exit_time: '15:10',
  stopPct: '',
  targetPct: '1.5',
  universe: 'reliance, infy\nTCS  ,, ',
  exchange: 'NSE',
  note: 'ignored',
}

describe('configFromForm', () => {
  it('turns the form into the config the server validates', () => {
    expect(configFromForm(FORM)).toEqual({
      capital: 500000,
      allocation: 0.6,
      leverage: 2,
      product: 'MIS',
      entry_time: '09:16',
      exit_time: '15:10',
      stop_pct: null,
      target_pct: 1.5,
      universe: ['RELIANCE', 'INFY', 'TCS'],
      exchange: 'NSE',
      execution_mode: 'backtest',
    })
  })

  it('sends a blank exit as no exit, so the server can refuse it for MIS', () => {
    expect(configFromForm({ ...FORM, exit_time: ' ' }).exit_time).toBeNull()
  })

  it('passes nonsense through as NaN rather than guessing a number', () => {
    expect(Number.isNaN(configFromForm({ ...FORM, capital: 'lots' }).capital)).toBe(true)
  })
})
