import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@/test/test-utils'
import { UploadStrategyFile } from './UploadStrategyFile'

const upload = vi.hoisted(() => vi.fn())
vi.mock('@/api/research', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/research')>()),
  uploadStrategyFile: upload,
}))

function show() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <UploadStrategyFile />
    </QueryClientProvider>
  )
}

describe('UploadStrategyFile', () => {
  it('uploads only once a file is chosen, and passes the replace choice', async () => {
    upload.mockResolvedValue({ id: 7, key: 'opening_range', version_hash: 'a'.repeat(64) })
    show()
    fireEvent.click(screen.getByRole('button', { name: 'Upload strategy file' }))
    const submit = screen.getByRole('button', { name: 'Upload' })
    expect(submit).toBeDisabled()

    const file = new File(['class Strategy: ...'], 'opening_range.py', { type: 'text/x-python' })
    fireEvent.change(screen.getByLabelText('Strategy file'), { target: { files: [file] } })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(submit)

    await waitFor(() => expect(upload).toHaveBeenCalledWith(file, true))
  })

  it('shows the contract a file must meet, in words a trader can act on', () => {
    show()
    fireEvent.click(screen.getByRole('button', { name: 'Upload strategy file' }))
    expect(screen.getByText(/defines a class named Strategy/)).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/paper|\barm(ed|ing)?\b/i)
  })
})
