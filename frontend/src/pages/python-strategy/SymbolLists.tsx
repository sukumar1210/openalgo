// pages/python-strategy/SymbolLists.tsx
// Named symbol lists: typed once, chosen per strategy on its Schedule tab, and
// handed to the script as OPENALGO_SYMBOLS when it starts. The dialog manages
// the lists; the card chooses one for a strategy. Both read one query, so a
// list saved in one is seen in the other.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ListChecks } from 'lucide-react'
import { useState } from 'react'
import { pythonStrategyApi } from '@/api/python-strategy'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { useStrategyExchanges } from '@/hooks/useStrategyExchanges'
import type { PythonStrategy, StrategySymbolList, SymbolList } from '@/types/python-strategy'
import { showToast } from '@/utils/toast'

export const symbolListsQueryKey = ['python-strategy', 'symbol-lists'] as const

function serverMessage(e: unknown, fallback: string): string {
  return (e as { response?: { data?: { message?: string } } }).response?.data?.message ?? fallback
}

const selectClass =
  'w-full px-3 py-2 text-sm border rounded-md bg-background focus:outline-none focus:ring-2 focus:ring-primary'

type Draft = { id?: string; name: string; exchange: string; symbols: string }

const EMPTY: Draft = { name: '', exchange: 'NSE', symbols: '' }

export function SymbolListsDialog() {
  const queryClient = useQueryClient()
  const { exchanges } = useStrategyExchanges()
  const [draft, setDraft] = useState<Draft | null>(null)
  const [error, setError] = useState<string | null>(null)
  const lists = useQuery({
    queryKey: symbolListsQueryKey,
    queryFn: pythonStrategyApi.listSymbolLists,
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: symbolListsQueryKey })
  const save = useMutation({
    mutationFn: (list: Draft) => pythonStrategyApi.saveSymbolList(list),
    onSuccess: (saved) => {
      refresh()
      setDraft(null)
      setError(null)
      showToast.success(`${saved.name} saved, ${saved.symbols.length} symbols`, 'pythonStrategy')
    },
    onError: (e) => setError(serverMessage(e, 'The list could not be saved. Try again.')),
  })
  const remove = useMutation({
    mutationFn: (id: string) => pythonStrategyApi.deleteSymbolList(id),
    onSuccess: refresh,
    onError: (e) =>
      showToast.error(serverMessage(e, 'Could not delete the list.'), 'pythonStrategy'),
  })

  const edit = (list: SymbolList) =>
    setDraft({
      id: list.id,
      name: list.name,
      exchange: list.exchange,
      symbols: list.symbols.join(', '),
    })

  return (
    <Dialog onOpenChange={(open) => !open && setDraft(null)}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <ListChecks className="h-4 w-4 mr-2" />
          Symbol lists
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Symbol lists</DialogTitle>
          <DialogDescription>
            Type a universe once and choose it for any strategy on its Schedule tab. The script
            reads it from OPENALGO_SYMBOLS, and a change reaches each strategy at its next start.
          </DialogDescription>
        </DialogHeader>
        {draft ? (
          <form
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault()
              save.mutate(draft)
            }}
          >
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="list-name">Name</Label>
                <Input
                  id="list-name"
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="list-exchange">Exchange</Label>
                <select
                  id="list-exchange"
                  className={selectClass}
                  value={draft.exchange}
                  onChange={(e) => setDraft({ ...draft, exchange: e.target.value })}
                >
                  {exchanges.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="list-symbols">Symbols</Label>
              <Textarea
                id="list-symbols"
                rows={6}
                placeholder="RELIANCE, TCS, HDFCBANK ... separated by commas, spaces or new lines"
                value={draft.symbols}
                onChange={(e) => setDraft({ ...draft, symbols: e.target.value })}
              />
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={() => setDraft(null)}>
                Cancel
              </Button>
              <Button type="submit" disabled={save.isPending}>
                {save.isPending ? 'Saving...' : 'Save list'}
              </Button>
            </div>
          </form>
        ) : (
          <div className="space-y-3">
            {(lists.data ?? []).length === 0 && (
              <p className="py-4 text-center text-sm text-muted-foreground">No lists yet.</p>
            )}
            {(lists.data ?? []).map((list) => (
              <div
                key={list.id}
                className="flex items-start justify-between gap-3 rounded-md border p-3"
              >
                <div className="min-w-0">
                  <p className="font-medium">
                    {list.name}{' '}
                    <span className="text-xs text-muted-foreground">
                      {list.exchange}, {list.symbols.length} symbols
                    </span>
                  </p>
                  <p className="truncate text-xs text-muted-foreground">
                    {list.symbols.join(', ')}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {list.used_by.length ? `Used by ${list.used_by.join(', ')}` : 'Not used yet'}
                  </p>
                </div>
                <div className="flex shrink-0 gap-2">
                  <Button size="sm" variant="outline" onClick={() => edit(list)}>
                    Edit
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={list.used_by.length > 0 || remove.isPending}
                    title={
                      list.used_by.length ? 'Choose another list for its strategies first' : ''
                    }
                    onClick={() => remove.mutate(list.id)}
                  >
                    Delete
                  </Button>
                </div>
              </div>
            ))}
            <Button onClick={() => setDraft(EMPTY)}>New list</Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

export function StrategySymbolsCard({
  strategy,
  onChanged,
}: {
  strategy: PythonStrategy
  onChanged: (list: StrategySymbolList | null) => void
}) {
  const queryClient = useQueryClient()
  const lists = useQuery({
    queryKey: symbolListsQueryKey,
    queryFn: pythonStrategyApi.listSymbolLists,
  })
  const running = strategy.status === 'running'
  const usable = (lists.data ?? []).filter((list) => list.exchange === strategy.exchange)
  const choose = useMutation({
    mutationFn: (listId: string | null) => pythonStrategyApi.setSymbolList(strategy.id, listId),
    onSuccess: (_, listId) => {
      const chosen = usable.find((list) => list.id === listId)
      onChanged(chosen ? { id: chosen.id, name: chosen.name, count: chosen.symbols.length } : null)
      queryClient.invalidateQueries({ queryKey: symbolListsQueryKey })
      showToast.success(chosen ? `Uses ${chosen.name}` : 'No symbol list', 'pythonStrategy')
    },
    onError: (e) =>
      showToast.error(serverMessage(e, 'Could not change the list.'), 'pythonStrategy'),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Symbols</CardTitle>
        <CardDescription>
          {running
            ? 'Stop the strategy to change this. It applies from the next start.'
            : `Handed to the script as OPENALGO_SYMBOLS when it starts. Lists are made under Symbol lists in Python Strategies; ${strategy.exchange} lists are shown.`}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <select
          aria-label="Symbol list"
          className={selectClass}
          disabled={running || choose.isPending}
          value={strategy.symbol_list?.id ?? ''}
          onChange={(e) => choose.mutate(e.target.value || null)}
        >
          <option value="">None: the script chooses its own symbols</option>
          {usable.map((list) => (
            <option key={list.id} value={list.id}>
              {list.name} ({list.symbols.length} symbols)
            </option>
          ))}
        </select>
      </CardContent>
    </Card>
  )
}
