import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import type { StrategyVersion } from '@/api/research'
import { fireEvent, render, screen, waitFor } from '@/test/test-utils'
import { VersionPropertiesDialog } from './VersionPropertiesDialog'

const saved = vi.hoisted(() => ({ calls: [] as unknown[] }))

vi.mock('@/api/research', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/research')>()
  return {
    ...actual,
    setVersionProperties: vi.fn(async (id: number, payload: unknown) => {
      saved.calls.push([id, payload])
      return {}
    }),
  }
})

const version = {
  id: 3,
  strategy_id: 1,
  version_hash: 'h',
  dist_name: 'alpha-a',
  dist_version: '0.2.5',
  code_hash: 'c',
  artifact_hash: null,
  model_hash: null,
  adapter_version: null,
  interval: '5m',
  meta: {},
  created_at: null,
  properties: {
    training_start: '2022-01-01',
    training_end: '2022-12-31',
    training_source: 'declared',
    properties: { Model: 'LightGBM' },
    updated_by: null,
    updated_at: null,
  },
} as StrategyVersion

function show() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <VersionPropertiesDialog version={version} onClose={() => {}} />
    </QueryClientProvider>
  )
}

describe('VersionPropertiesDialog', () => {
  it('says what the strategy declares and saves the period and properties set here', async () => {
    show()
    expect(
      screen.getByText(/The strategy declares it was trained on 2022-01-01 to 2022-12-31/)
    ).toBeInTheDocument()
    expect(screen.getByDisplayValue('LightGBM')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Trained on data from'), {
      target: { value: '2021-01-01' },
    })
    fireEvent.change(screen.getByLabelText('to'), { target: { value: '2021-06-30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add a property' }))
    const names = screen.getAllByLabelText('Property name')
    fireEvent.change(names[1], { target: { value: 'Features' } })
    fireEvent.change(screen.getAllByLabelText('Property value')[1], {
      target: { value: 'ATR, VWAP' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(saved.calls).toHaveLength(1))
    expect(saved.calls[0]).toEqual([
      3,
      {
        training_start: '2021-01-01',
        training_end: '2021-06-30',
        properties: [
          { name: 'Model', value: 'LightGBM' },
          { name: 'Features', value: 'ATR, VWAP' },
        ],
      },
    ])
  })
})
