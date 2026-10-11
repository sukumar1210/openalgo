// pages/research/UploadStrategyFile.tsx
// Add a strategy as one Python file, the way /python takes a script. The server
// checks the file in a process of its own before keeping it, so a file that does
// not build a strategy is refused here with what to fix.

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { researchQueryKeys, uploadStrategyFile } from '@/api/research'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
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

const EXAMPLE = `class Strategy:
    interval = "5m"          # 1m, 5m, 15m, 30m, 1h or D

    def decide(self, ctx):
        # Called on every closed bar inside the entry window.
        # Return {symbol: weight}: 1.0 is all of the sized
        # capital long, -1.0 short; None keeps what you hold.
        bars = ctx.bars(ctx.symbols[0])
        return {ctx.symbols[0]: 1.0} if bars.close[-1] > bars.open[-1] else {}`

export function UploadStrategyFile() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [replace, setReplace] = useState(false)
  const upload = useMutation({
    mutationFn: () => uploadStrategyFile(file as File, replace),
    onSuccess: (item) => {
      queryClient.invalidateQueries({ queryKey: researchQueryKeys.all })
      showToast.success(`${item.key} is ready. Save a configuration to backtest it.`)
      setOpen(false)
      navigate(`/research/strategies/${item.id}`)
    },
    onError: (err: Error) => showToast.error(err.message),
  })

  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        Upload strategy file
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Upload a strategy file</DialogTitle>
            <DialogDescription>
              One Python file that defines a class named Strategy. Its file name becomes its key, so
              name it with lowercase letters, digits and underscores, like opening_range.py. It
              backtests and deploys like any other strategy here.
            </DialogDescription>
          </DialogHeader>
          <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs">{EXAMPLE}</pre>
          <div className="space-y-1.5">
            <Label htmlFor="research-strategy-file">Strategy file</Label>
            <Input
              id="research-strategy-file"
              type="file"
              accept=".py"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>
          <div className="flex items-start gap-2">
            <Checkbox
              id="research-strategy-replace"
              checked={replace}
              onCheckedChange={(value) => setReplace(value === true)}
            />
            <Label htmlFor="research-strategy-replace" className="text-sm font-normal">
              Replace a file with the same name. It becomes a new version; earlier versions and
              their results are kept.
            </Label>
          </div>
          <DialogFooter>
            <Button disabled={!file || upload.isPending} onClick={() => upload.mutate()}>
              {upload.isPending ? 'Checking the file...' : 'Upload'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
