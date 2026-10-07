// api/research.ts
// Research: strategy registry, config versions, experiments and backtest runs.
// Session-authenticated UI endpoints under /research/api (blueprints/research.py).

import { webClient } from './client'

export interface StrategyVersion {
  id: number
  strategy_id: number
  version_hash: string
  dist_name: string
  dist_version: string
  code_hash: string
  artifact_hash: string | null
  interval: string
  meta: Record<string, unknown>
  created_at: string | null
}

export interface ResearchConfig {
  capital: number
  allocation: number
  leverage: number
  product: 'MIS' | 'CNC'
  entry_time: string
  exit_time: string | null
  stop_pct: number | null
  target_pct: number | null
  universe: string[]
  exchange: string
  execution_mode: 'backtest'
}

export interface ConfigVersion {
  id: number
  strategy_id: number
  parent_id: number | null
  version_no: number
  config: ResearchConfig
  config_hash: string
  note: string | null
  created_at: string | null
}

export interface ResearchStrategy {
  id: number
  key: string
  source: 'builtin' | 'entry_point'
  created_at: string | null
}

export interface StrategyListItem extends ResearchStrategy {
  latest_version: StrategyVersion | null
  latest_config: ConfigVersion | null
}

export interface StrategyProblem {
  key: string
  message: string
}

export interface StrategyDetail extends ResearchStrategy {
  versions: StrategyVersion[]
  /** used_by: how many scenarios use this configuration version. */
  configs: (ConfigVersion & { used_by: number })[]
}

interface Envelope {
  status: 'success' | 'error'
  message?: string
}

export type RunStatus = 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED'
export type ScenarioMode = 'ISOLATED' | 'SHARED'

export interface RunMetrics {
  capital: number
  net_pnl: number
  gross_pnl: number
  return_pct: number | null
  trades: number
  winning_trades: number
  losing_trades: number
  win_rate: number | null
  profit_factor: number | null
  avg_trade: number | null
  fees: number
  slippage: number
  costs: number
  turnover: number | null
  max_drawdown_pct: number
  cagr_pct: number | null
  sharpe: number | null
  sortino: number | null
  days: number
  conflicts?: number
}

export interface AttributionRow extends RunMetrics {
  instance: number
  label: string
  portfolio: number
  reduced_by_limit: number
}

export interface ResearchRun {
  id: number
  scenario_id: number
  run_hash: string | null
  status: RunStatus
  progress: number
  message: string | null
  engine_version: string | null
  engine_hash: string | null
  metrics: RunMetrics | null
  attribution: AttributionRow[] | null
  queued_at: string | null
  started_at: string | null
  finished_at: string | null
  environment?: 'BACKTEST' | 'SANDBOX'
  trading_date?: string | null
}

export interface ScenarioInstance {
  position: number
  strategy_id: number
  strategy_key: string
  strategy_version: StrategyVersion
  config_version: ConfigVersion
}

export interface Scenario {
  id: number
  experiment_id: number
  name: string
  mode: ScenarioMode
  dataset_spec: {
    symbols: string[]
    exchange: string
    interval: string
    start: string
    end: string
  }
  dataset_id: number | null
  cost_model: { preset: string; slippage_bps: number }
  execution_model: { fill: string }
  risk_policy: { max_gross_leverage: number }
  shared_capital: number | null
  created_at: string | null
  instances: ScenarioInstance[]
  runs?: ResearchRun[]
}

export interface Experiment {
  id: number
  name: string
  description: string | null
  created_at: string | null
}

export interface ExperimentListItem extends Experiment {
  scenarios: number
  runs: Partial<Record<RunStatus, number>>
}

export interface ExperimentDetail extends Experiment {
  scenarios: Scenario[]
}

export interface RunDetail extends ResearchRun {
  scenario: Scenario
  dataset: { dataset_hash: string; bar_count: number; spec: Record<string, unknown> } | null
  artifacts: { kind: string; sha256: string; size_bytes: number }[]
}

export interface LedgerTrade {
  seq: number
  instance: number
  symbol: string
  side: 'LONG' | 'SHORT'
  qty: number
  entry_ts: number
  entry_price: number
  exit_ts: number
  exit_price: number
  gross_pnl: number
  fees: number
  slippage: number
  net_pnl: number
}

export interface EquityCurves {
  timestamps: number[]
  series: Record<string, number[]>
}

export interface ScenarioPayload {
  name: string
  mode: ScenarioMode
  start: string
  end: string
  cost_model: { preset: string; slippage_bps: number }
  risk_policy: { max_gross_leverage: number }
  shared_capital: number | null
  instances: { strategy_version_id: number; config_version_id: number }[]
}

export const COST_PRESETS: { value: string; label: string }[] = [
  { value: 'india_intraday_nse', label: 'India intraday (NSE)' },
  { value: 'india_intraday_bse', label: 'India intraday (BSE)' },
  { value: 'india_delivery_nse', label: 'India delivery (NSE)' },
  { value: 'india_delivery_bse', label: 'India delivery (BSE)' },
]

export const ACTIVE_STATUSES: RunStatus[] = ['QUEUED', 'RUNNING']

export type SandboxStatus =
  | 'QUEUED'
  | 'STARTING'
  | 'RUNNING'
  | 'PAUSED'
  | 'STOPPING'
  | 'COMPLETED'
  | 'FAILED'
  | 'CANCELLED'

export const SANDBOX_ACTIVE: SandboxStatus[] = [
  'QUEUED',
  'STARTING',
  'RUNNING',
  'PAUSED',
  'STOPPING',
]

export interface Deployment {
  id: number
  scenario_id: number
  name: string
  status: 'ACTIVE' | 'STOPPED'
  schedule_start: string
  schedule_stop: string
  schedule_days: string[]
  stale_policy: 'PAUSE' | 'FAIL_SAFE'
  created_at: string | null
  stopped_at: string | null
}

export interface SessionSummary {
  id: number
  status: SandboxStatus
  message: string | null
  trading_date: string | null
  metrics: RunMetrics | null
  heartbeat_at: string | null
  started_at: string | null
  finished_at: string | null
}

export interface DeploymentListItem extends Deployment {
  latest_session: SessionSummary | null
}

export interface DeploymentDetail extends Deployment {
  scenario: Scenario
  sessions: SessionSummary[]
  totals: { sessions: number; net_pnl: number; trades: number; costs: number }
}

export interface LiveBook {
  instance: number
  label: string
  portfolio: number
  capital: number
  cash: number
  equity: number
  positions: Record<string, { qty: number; avg: number | null; mark: number | null }>
}

export interface LiveState {
  market_data: { status: 'CONNECTING' | 'WAITING' | 'LIVE' | 'STALE'; last_tick_at: number | null }
  accept_new?: boolean
  books?: LiveBook[]
  open_orders?: { platform_order_id: string; symbol: string; net: number }[]
  last_signal?: { symbol: string; net: number; bar_ts: number } | null
  last_fill?: { symbol: string; qty: number; price: number; instance: number; ts: number } | null
  conflicts?: number
  last_bar_ts?: number | null
  updated_at?: number
}

export interface SessionEvent {
  seq: number
  kind: string
  at: string
  bar_ts: number | null
  instance: number | null
  platform_order_id: string | null
  payload: Record<string, unknown>
}

export interface SandboxSession extends SessionSummary {
  scenario_id: number
  deployment_id: number
  attribution: AttributionRow[] | null
  live_state: LiveState | null
  provenance: Record<string, unknown> | null
  events: SessionEvent[]
}

export interface DeploymentPayload {
  scenario_id: number
  name: string
  schedule_start: string
  schedule_stop: string
  schedule_days: string[]
  stale_policy: 'PAUSE' | 'FAIL_SAFE'
}

export const researchQueryKeys = {
  all: ['research'] as const,
  strategies: () => [...researchQueryKeys.all, 'strategies'] as const,
  strategy: (id: number) => [...researchQueryKeys.strategies(), id] as const,
  experiments: () => [...researchQueryKeys.all, 'experiments'] as const,
  experiment: (id: number) => [...researchQueryKeys.experiments(), id] as const,
  run: (id: number) => [...researchQueryKeys.all, 'runs', id] as const,
  trades: (id: number, instance: number | null) =>
    [...researchQueryKeys.run(id), 'trades', instance] as const,
  equity: (id: number) => [...researchQueryKeys.run(id), 'equity'] as const,
  deployments: () => [...researchQueryKeys.all, 'deployments'] as const,
  deployment: (id: number) => [...researchQueryKeys.deployments(), id] as const,
  session: (id: number) => [...researchQueryKeys.all, 'sessions', id] as const,
}

/**
 * Run one request and return its body, or throw with the server's own message.
 * The blueprint answers every refusal with a sentence written for the trader,
 * on 4xx and 5xx alike, so that sentence is what reaches the toast.
 */
async function call<T extends Envelope>(
  request: Promise<{ data: T }>,
  fallback: string
): Promise<T> {
  let data: T
  try {
    data = (await request).data
  } catch (error) {
    const message = (error as { response?: { data?: { message?: string } } })?.response?.data
      ?.message
    throw new Error(message || fallback)
  }
  if (data.status !== 'success') throw new Error(data.message || fallback)
  return data
}

export async function listStrategies(): Promise<{
  items: StrategyListItem[]
  problems: StrategyProblem[]
}> {
  const data = await call(
    webClient.get<Envelope & { items: StrategyListItem[]; problems: StrategyProblem[] }>(
      '/research/api/strategies'
    ),
    'Could not load strategies'
  )
  return { items: data.items, problems: data.problems }
}

export async function getStrategy(id: number): Promise<StrategyDetail> {
  const data = await call(
    webClient.get<Envelope & { item: StrategyDetail }>(`/research/api/strategies/${id}`),
    'Could not load the strategy'
  )
  return data.item
}

export async function saveConfig(
  strategyId: number,
  config: ResearchConfig,
  parentId: number | null,
  note?: string
): Promise<{ item: ConfigVersion; created: boolean }> {
  const data = await call(
    webClient.post<Envelope & { item: ConfigVersion; created: boolean }>(
      `/research/api/strategies/${strategyId}/configs`,
      { config, parent_id: parentId, note }
    ),
    'Could not save the configuration'
  )
  return { item: data.item, created: data.created }
}

export async function listExperiments(): Promise<ExperimentListItem[]> {
  const data = await call(
    webClient.get<Envelope & { items: ExperimentListItem[] }>('/research/api/experiments'),
    'Could not load experiments'
  )
  return data.items
}

export async function createExperiment(name: string, description: string): Promise<Experiment> {
  const data = await call(
    webClient.post<Envelope & { item: Experiment }>('/research/api/experiments', {
      name,
      description,
    }),
    'Could not create the experiment'
  )
  return data.item
}

export async function getExperiment(id: number): Promise<ExperimentDetail> {
  const data = await call(
    webClient.get<Envelope & { item: ExperimentDetail }>(`/research/api/experiments/${id}`),
    'Could not load the experiment'
  )
  return data.item
}

export async function createScenario(
  experimentId: number,
  payload: ScenarioPayload
): Promise<Scenario> {
  const data = await call(
    webClient.post<Envelope & { item: Scenario }>(
      `/research/api/experiments/${experimentId}/scenarios`,
      payload
    ),
    'Could not save the scenario'
  )
  return data.item
}

export async function runScenario(
  scenarioId: number
): Promise<{ item: ResearchRun; created: boolean }> {
  const data = await call(
    webClient.post<Envelope & { item: ResearchRun; created: boolean }>(
      `/research/api/scenarios/${scenarioId}/runs`
    ),
    'Could not start the backtest'
  )
  return { item: data.item, created: data.created }
}

export async function runExperiment(experimentId: number): Promise<ResearchRun[]> {
  const data = await call(
    webClient.post<Envelope & { items: ResearchRun[] }>(
      `/research/api/experiments/${experimentId}/runs`
    ),
    'Could not start the experiment'
  )
  return data.items
}

export async function getRun(id: number): Promise<RunDetail> {
  const data = await call(
    webClient.get<Envelope & { item: RunDetail }>(`/research/api/runs/${id}`),
    'Could not load the run'
  )
  return data.item
}

export async function cancelRun(id: number): Promise<ResearchRun> {
  const data = await call(
    webClient.post<Envelope & { item: ResearchRun }>(`/research/api/runs/${id}/cancel`),
    'Could not cancel the run'
  )
  return data.item
}

export async function getRunTrades(
  id: number,
  instance: number | null
): Promise<{ total: number; items: LedgerTrade[] }> {
  const params = instance === null ? '' : `?instance=${instance}`
  const data = await call(
    webClient.get<Envelope & { total: number; items: LedgerTrade[] }>(
      `/research/api/runs/${id}/trades${params}`
    ),
    'Could not load the trades'
  )
  return { total: data.total, items: data.items }
}

export async function getRunEquity(id: number): Promise<EquityCurves> {
  const data = await call(
    webClient.get<Envelope & { item: EquityCurves }>(`/research/api/runs/${id}/equity`),
    'Could not load the equity curve'
  )
  return data.item
}

export async function listDeployments(): Promise<DeploymentListItem[]> {
  const data = await call(
    webClient.get<Envelope & { item: DeploymentListItem[] }>('/research/api/deployments'),
    'Could not load Sandbox deployments'
  )
  return data.item
}

export async function createDeployment(payload: DeploymentPayload): Promise<Deployment> {
  const data = await call(
    webClient.post<Envelope & { item: Deployment }>('/research/api/deployments', payload),
    'Could not deploy to the Sandbox'
  )
  return data.item
}

export async function getDeployment(id: number): Promise<DeploymentDetail> {
  const data = await call(
    webClient.get<Envelope & { item: DeploymentDetail }>(`/research/api/deployments/${id}`),
    'Could not load the deployment'
  )
  return data.item
}

export async function deploymentAction(
  id: number,
  action: 'stop' | 'activate' | 'start-now'
): Promise<unknown> {
  const data = await call(
    webClient.post<Envelope & { item: unknown }>(`/research/api/deployments/${id}/${action}`),
    'The deployment could not be updated'
  )
  return data.item
}

export async function getSandboxSession(id: number): Promise<SandboxSession> {
  const data = await call(
    webClient.get<Envelope & { item: SandboxSession }>(`/research/api/sessions/${id}`),
    'Could not load the session'
  )
  return data.item
}

export async function controlSession(
  id: number,
  action: 'pause' | 'resume' | 'stop'
): Promise<SandboxSession> {
  const data = await call(
    webClient.post<Envelope & { item: SandboxSession }>(`/research/api/sessions/${id}/${action}`),
    'The session could not be updated'
  )
  return data.item
}

export const SESSION_STATUS_LABEL: Record<SandboxStatus, string> = {
  QUEUED: 'Waiting',
  STARTING: 'Starting',
  RUNNING: 'Running',
  PAUSED: 'Paused',
  STOPPING: 'Closing positions',
  COMPLETED: 'Completed',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
}

export function formatIstTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

export function formatPct(value: number | null | undefined, digits = 2): string {
  return value == null ? 'n/a' : `${value.toFixed(digits)}%`
}

export function formatNumber(value: number | null | undefined, digits = 2): string {
  return value == null ? 'n/a' : value.toFixed(digits)
}

export function shortHash(hash: string | null | undefined): string {
  return hash ? hash.slice(0, 10) : '-'
}

export function formatRupees(value: number): string {
  return `Rs ${value.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
}
