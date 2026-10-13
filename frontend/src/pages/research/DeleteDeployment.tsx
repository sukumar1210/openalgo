// pages/research/DeleteDeployment.tsx
// Delete a stopped deployment from the page. Its past sessions are kept and
// still open from their own links; the server refuses one that is active or
// has a session running.

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { deploymentAction, researchQueryKeys } from '@/api/research'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { showToast } from '@/utils/toast'

export function DeleteDeployment({
  id,
  name,
  stopped,
  onDeleted,
}: {
  id: number
  name: string
  stopped: boolean
  onDeleted?: () => void
}) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const remove = useMutation({
    mutationFn: () => deploymentAction(id, 'delete'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: researchQueryKeys.deployments() })
      showToast.success(`${name} deleted`)
      onDeleted?.()
    },
    onError: (err: Error) => showToast.error(err.message),
  })

  return (
    <>
      <span title={stopped ? undefined : 'Stop the deployment before deleting it'}>
        <Button
          size="sm"
          variant="ghost"
          disabled={!stopped || remove.isPending}
          onClick={() => setOpen(true)}
        >
          Delete
        </Button>
      </span>
      <AlertDialog open={open} onOpenChange={setOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {name}?</AlertDialogTitle>
            <AlertDialogDescription>
              It leaves this page and will never start a session again. The sessions it already ran
              are kept, with their trades and results.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => remove.mutate()}>Delete</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
