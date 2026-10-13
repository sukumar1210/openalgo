import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@/test/test-utils'
import { DeleteDeployment } from './DeleteDeployment'

const action = vi.hoisted(() => vi.fn())
vi.mock('@/api/research', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/research')>()),
  deploymentAction: action,
}))

function show(stopped: boolean, onDeleted = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <DeleteDeployment id={4} name="res1 paper" stopped={stopped} onDeleted={onDeleted} />
    </QueryClientProvider>
  )
  return onDeleted
}

describe('DeleteDeployment', () => {
  it('deletes a stopped deployment once confirmed, saying its sessions are kept', async () => {
    action.mockResolvedValue({})
    const onDeleted = show(true)
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(screen.getByText(/sessions it already ran are kept/)).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' }).at(-1) as HTMLElement)
    await waitFor(() => expect(action).toHaveBeenCalledWith(4, 'delete'))
    await waitFor(() => expect(onDeleted).toHaveBeenCalled())
  })

  it('is off for a deployment that is still active, saying why', () => {
    show(false)
    const button = screen.getByRole('button', { name: 'Delete' })
    expect(button).toBeDisabled()
    expect(button.parentElement).toHaveAttribute('title', 'Stop the deployment before deleting it')
  })
})
