// pages/research/VersionPropertiesDialog.tsx
// What is known about one strategy version: the period its model was trained
// on, and anything else worth recording (the model, its features, notes).
// Descriptive only (master PRD section 5): nothing here changes how the
// version runs. The training period is what marks a backtest over the same
// dates as in-sample on its run page.

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { researchQueryKeys, type StrategyVersion, setVersionProperties } from '@/api/research'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { showToast } from '@/utils/toast'

interface Row {
  name: string
  value: string
}

export function VersionPropertiesDialog({
  version,
  onClose,
}: {
  version: StrategyVersion | null
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [rows, setRows] = useState<Row[]>([])
  const props = version?.properties

  useEffect(() => {
    if (!version) return
    const own = props?.training_source === 'set'
    setStart(own ? (props?.training_start ?? '') : '')
    setEnd(own ? (props?.training_end ?? '') : '')
    setRows(Object.entries(props?.properties ?? {}).map(([name, value]) => ({ name, value })))
  }, [version, props])

  const save = useMutation({
    mutationFn: () =>
      setVersionProperties(version?.id ?? 0, {
        training_start: start || null,
        training_end: end || null,
        properties: rows,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: researchQueryKeys.all })
      showToast.success('Properties saved')
      onClose()
    },
    onError: (err: Error) => showToast.error(err.message),
  })

  const setRow = (index: number, change: Partial<Row>) =>
    setRows((current) => current.map((row, i) => (i === index ? { ...row, ...change } : row)))

  return (
    <Dialog open={version !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>
            Properties of {version?.dist_name} {version?.dist_version}
          </DialogTitle>
          <DialogDescription>
            What is known about this version: the data its model was trained on, and anything else
            worth recording. Nothing here changes how it runs. A backtest over the training period
            is marked in-sample on its run page.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="training-start">Trained on data from</Label>
              <Input
                id="training-start"
                type="date"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="training-end">to</Label>
              <Input
                id="training-end"
                type="date"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </div>
          </div>
          {props?.training_source === 'declared' && (
            <p className="text-xs text-muted-foreground">
              The strategy declares it was trained on {props.training_start} to {props.training_end}
              . Set a period here to replace that.
            </p>
          )}

          <div className="space-y-2">
            <Label>Other properties</Label>
            {rows.length === 0 && (
              <p className="text-xs text-muted-foreground">
                None yet. Add the model, its features, a note: anything a later reader needs.
              </p>
            )}
            {rows.map((row, index) => (
              <div key={index} className="flex gap-2">
                <Input
                  aria-label="Property name"
                  placeholder="Name"
                  className="w-40"
                  value={row.name}
                  onChange={(e) => setRow(index, { name: e.target.value })}
                />
                <Input
                  aria-label="Property value"
                  placeholder="Value"
                  value={row.value}
                  onChange={(e) => setRow(index, { value: e.target.value })}
                />
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setRows((current) => current.filter((_, i) => i !== index))}
                >
                  Remove
                </Button>
              </div>
            ))}
            <Button
              variant="outline"
              size="sm"
              onClick={() => setRows((current) => [...current, { name: '', value: '' }])}
            >
              Add a property
            </Button>
          </div>

          {props?.updated_by && (
            <p className="text-xs text-muted-foreground">
              Last changed by {props.updated_by} on {props.updated_at?.slice(0, 10)}.
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={save.isPending} onClick={() => save.mutate()}>
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
