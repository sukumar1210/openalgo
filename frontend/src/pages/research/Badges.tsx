// pages/research/Badges.tsx
// The status badges the run and session pages share.

import { type ResearchRun, type SandboxStatus, SESSION_STATUS_LABEL } from '@/api/research'
import { Badge } from '@/components/ui/badge'

const STATUS_LABEL: Record<ResearchRun['status'], string> = {
  QUEUED: 'Queued',
  RUNNING: 'Running',
  COMPLETED: 'Completed',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
}

export function StatusBadge({ status }: { status: ResearchRun['status'] }) {
  const variant =
    status === 'FAILED' ? 'destructive' : status === 'COMPLETED' ? 'default' : 'secondary'
  return <Badge variant={variant}>{STATUS_LABEL[status]}</Badge>
}

export function SessionStatusBadge({ status }: { status: SandboxStatus }) {
  const variant =
    status === 'FAILED' ? 'destructive' : status === 'RUNNING' ? 'default' : 'secondary'
  return <Badge variant={variant}>{SESSION_STATUS_LABEL[status]}</Badge>
}
