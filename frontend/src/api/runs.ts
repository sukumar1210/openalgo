// api/runs.ts
// Runs: strategies trading together in the Sandbox with money of their own,
// from blueprints/runs.py. Every refusal comes back as a 400 whose message is
// written for the trader; errorMessage() reads it.

import { webClient } from './client'
import type { PerformanceSummary } from './strategy-dashboard'

export type RunStatus = 'draft' | 'active' | 'paused' | 'stopped'
export type RunMode = 'isolated' | 'shared'
export type RunPolicy = 'reset' | 'own' | 'pooled'
export type RunLimit = 'capital' | 'fixed'
export type AllocationKind = 'fixed' | 'percent'
export type Sizing = 'platform' | 'self'

export interface RunMember {
  id: number
  seat: string
  label: string
  script_id: string
  allocation_kind: AllocationKind
  allocation_value: number
  /** In rupees, worked out from the Run's capital. */
  allocated: number
  sizing: Sizing
  capital_dependent: boolean
  settings: {
    exchange: string
    schedule: { start: string; stop: string | null; days: string[] }
    symbols: string[]
    script_name: string
    file_name: string
    code_hash: string
  }
  fingerprint: string
  order_name: string
}

export interface RunVersion {
  number: number
  effective_from: string
  capital: number
  mode: RunMode
  policy: RunPolicy
  limit: RunLimit
  loss_limit_pct: number | null
  members: RunMember[]
}

export interface RunDayRow {
  seat: string
  trade_date: string
  opening_capital: number
  transfer: number
  limit: number
  closing_balance: number | null
  gross: number | null
  charges: number | null
  net: number | null
  trades: number | null
  closed: boolean
}

export interface OpenPosition {
  symbol: string
  exchange: string
  quantity: number
  average_price: number
  ltp: number
  pnl: number
}

export interface SeatToday {
  seat: string
  label: string
  opening: number | null
  transfer: number | null
  limit: number | null
  loss_limit: number | null
  stopped: boolean
  net: number | null
  charges: number | null
  trades: number
  exposure: number
  positions: OpenPosition[]
}

export interface RunToday {
  date: string
  /** Whether today's open has happened (09:05 on a trading day). */
  opened: boolean
  seats: SeatToday[]
  members: { seat: string; label: string; capital: number }[]
}

export interface CopyRow {
  time: string | null
  member: string
  seat: string | null
  symbol: string | null
  action: 'BUY' | 'SELL'
  quantity: number
  price: number | null
  status: 'claimed' | 'placed' | 'refused'
  reason: string | null
}

export interface RunSource {
  name: string
  fingerprint: string
  status: string
  running: boolean
  reference_capital: number
  schedule: { start: string; stop: string | null; days: string[] } | null
  /** The Runs that copy it today. */
  runs: string[]
}

export interface RunEvent {
  kind: string
  detail: Record<string, unknown>
  at: string | null
}

export interface Run {
  id: number
  name: string
  status: RunStatus
  version: RunVersion
  /** An edit that applies from a later day, if one is saved. */
  upcoming: RunVersion | null
  accounts: Record<string, string>
  days: RunDayRow[]
  today: RunToday
  copies?: CopyRow[]
  sources?: RunSource[]
  events?: RunEvent[]
}

export interface MemberInput {
  script_id: string
  label?: string
  allocation_kind: AllocationKind
  allocation_value: number
  sizing: Sizing
  capital_dependent?: boolean
}

export interface RunDefinition {
  name?: string
  capital: number
  mode: RunMode
  policy: RunPolicy
  limit: RunLimit
  loss_limit_pct: number | null
  members: MemberInput[]
}

/** One day of a P&L series: every figure after charges except ``pnl``. */
export interface PnlDay {
  date: string
  opening: number
  transfer: number
  /** Before charges. */
  pnl: number
  charges: number
  net: number
  trades: number
  cumulative: number
}

export interface PnlSeries {
  key: string
  name: string
  kind: 'run' | 'member' | 'main' | 'python' | 'module' | 'other' | 'combined' | 'backtest'
  daily: PnlDay[]
  summary: PerformanceSummary
}

export interface PnlData {
  series: PnlSeries[]
  combined: PnlSeries
}

export interface PnlQuery {
  runs?: number[]
  members?: [number, string][]
  main?: boolean
  strategies?: string[]
  from?: string
  to?: string
}

export interface Backtest {
  id: number
  run_id: number
  version: number | null
  from: string
  to: string
  status: 'running' | 'done' | 'failed'
  message: string | null
  replays: {
    fingerprint: string
    research_run_id: number
    status: string | null
    progress: number | null
    message: string | null
  }[]
  summary: PerformanceSummary | null
  created_at: string | null
  finished_at: string | null
  result?: {
    run: PnlSeries
    members: PnlSeries[]
    refused: number
    stopped: { date: string; seat: string }[]
  } | null
}

export interface AccountRow {
  token: string
  kind: 'run' | 'member' | 'source'
  label: string
  status: 'active' | 'closed'
  starting_capital: number
  equity: number | null
  charges_today: number
  daily_loss_limit: number | null
  run: { id: number; name: string | null } | null
  opened: string | null
}

export interface AccountsOverview {
  main: { label: string; starting_capital: number; equity: number | null }
  accounts: AccountRow[]
}

export interface AccountEvent {
  date: string | null
  kind: string
  amount: number
  reason: string | null
  reference: string | null
}

export const runKeys = {
  all: ['runs'] as const,
  list: () => ['runs', 'list'] as const,
  run: (id: number) => ['runs', 'run', id] as const,
  sources: () => ['runs', 'sources'] as const,
  pnl: (query: PnlQuery) => ['runs', 'pnl', JSON.stringify(query)] as const,
  backtests: (id: number) => ['runs', 'backtests', id] as const,
  backtest: (id: number, backtestId: number) => ['runs', 'backtest', id, backtestId] as const,
  accounts: () => ['runs', 'accounts'] as const,
  accountEvents: (token: string) => ['runs', 'account-events', token] as const,
}

/** The trader-facing message of a refused request, or ``fallback``. */
export function errorMessage(error: unknown, fallback: string): string {
  const message = (error as { response?: { data?: { message?: string } } })?.response?.data?.message
  return message || fallback
}

const base = '/runs/api'

export async function listRuns() {
  const response = await webClient.get<{ data: Run[] }>(`${base}/runs`)
  return response.data.data
}

export async function getRun(id: number) {
  const response = await webClient.get<{ data: Run }>(`${base}/runs/${id}`)
  return response.data.data
}

export async function createRun(definition: RunDefinition) {
  const response = await webClient.post<{ data: Run }>(`${base}/runs`, definition)
  return response.data.data
}

export async function editRun(id: number, definition: RunDefinition) {
  const response = await webClient.put<{ data: Run }>(`${base}/runs/${id}`, definition)
  return response.data.data
}

export type RunAction = 'start' | 'pause' | 'kill' | 'stop'

export async function actOnRun(id: number, action: RunAction) {
  const response = await webClient.post<{ data: Run }>(`${base}/runs/${id}/${action}`)
  return response.data.data
}

export async function deleteRun(id: number) {
  await webClient.delete(`${base}/runs/${id}`)
}

export async function getSources() {
  const response = await webClient.get<{ data: RunSource[] }>(`${base}/sources`)
  return response.data.data
}

export function pnlParams(query: PnlQuery): URLSearchParams {
  const params = new URLSearchParams()
  if (query.runs?.length) params.set('runs', query.runs.join(','))
  if (query.members?.length)
    params.set('members', query.members.map(([run, seat]) => `${run}:${seat}`).join(','))
  if (query.main) params.set('main', '1')
  if (query.strategies?.length) params.set('strategies', query.strategies.join(','))
  if (query.from) params.set('from', query.from)
  if (query.to) params.set('to', query.to)
  return params
}

export async function getPnl(query: PnlQuery) {
  const response = await webClient.get<{ data: PnlData }>(`${base}/pnl?${pnlParams(query)}`)
  return response.data.data
}

export async function listBacktests(id: number) {
  const response = await webClient.get<{ data: Backtest[] }>(`${base}/runs/${id}/backtests`)
  return response.data.data
}

export async function startBacktest(id: number, from: string, to: string) {
  const response = await webClient.post<{ data: Backtest }>(`${base}/runs/${id}/backtests`, {
    from,
    to,
  })
  return response.data.data
}

export async function getBacktest(id: number, backtestId: number) {
  const response = await webClient.get<{ data: Backtest }>(
    `${base}/runs/${id}/backtests/${backtestId}`
  )
  return response.data.data
}

export async function getAccounts() {
  const response = await webClient.get<{ data: AccountsOverview }>(`${base}/accounts`)
  return response.data.data
}

export async function getAccountEvents(token: string) {
  const response = await webClient.get<{ data: AccountEvent[] }>(
    `${base}/accounts/${encodeURIComponent(token)}/events`
  )
  return response.data.data
}

export const MODE_LABEL: Record<RunMode, string> = { isolated: 'Isolated', shared: 'Shared' }
export const POLICY_LABEL: Record<RunPolicy, string> = {
  reset: 'Reset daily',
  own: 'Own result',
  pooled: 'Pooled',
}
export const STATUS_LABEL: Record<RunStatus, string> = {
  draft: 'Draft',
  active: 'Active',
  paused: 'Paused',
  stopped: 'Stopped',
}
