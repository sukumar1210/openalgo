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
  model_hash: string | null
  adapter_version: string | null
  /** Present on a strategy's page: whether the operator allows it to trade live. */
  live_approved?: boolean
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

export type StrategyStatus = 'ready' | 'deployed' | 'trading' | 'not_installed'

export interface StrategyListItem extends ResearchStrategy {
  latest_version: StrategyVersion | null
  latest_config: ConfigVersion | null
  status: StrategyStatus
  active_deployments: number
  /** The latest finished run or session that included it, and its own P&L there. */
  last_run: {
    id: number
    status: RunStatus
    environment: string
    finished_at: string | null
    net_pnl: number | null
  } | null
}

export interface StrategyProblem {
  key: string
  message: string
}

/** How one configuration did: its own book in one finished run or session. */
export interface ConfigResult {
  run_id: number
  scenario_id: number
  scenario: string
  mode: ScenarioMode
  environment: string
  trading_date: string | null
  net_pnl: number
  return_pct: number | null
  max_drawdown_pct: number
  trades: number
  finished_at: string | null
}

export interface StrategyDetail extends ResearchStrategy {
  versions: StrategyVersion[]
  /** used_by: how many scenarios use this configuration version. */
  configs: (ConfigVersion & { used_by: number; results: ConfigResult[] })[]
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
  final_equity?: number
  avg_exposure_pct?: number | null
  max_exposure_pct?: number | null
  capital_used_pct?: number | null
  time_in_market_pct?: number | null
  /** Present when a run has more than one strategy. */
  together?: TogetherMetrics
  limit_breaches?: LimitBreach[]
}

export interface TogetherMetrics {
  order_groups: number
  conflict_rate_pct: number | null
  netting: { orders_avoided: number; notional_netted: number; costs_saved: number }
  overlap: { a: number; b: number; same_pct: number; opposite_pct: number }[]
  correlation: { a: number; b: number; value: number | null }[]
  reductions: number
}

export interface LimitBreach {
  portfolio: number
  ts: number
  reason: 'daily_loss' | 'daily_target' | 'lock_profit'
  day_pnl: number
  detail: string
}

export interface RiskPolicy {
  max_gross_leverage: number
  daily_loss_limit?: number
  daily_profit_target?: number
  lock_profit_at?: number
  lock_profit_floor?: number
  lock_trail_step?: number
}

export interface AttributionRow extends RunMetrics {
  instance: number
  label: string
  contribution_pct?: number | null
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
  environment?: 'BACKTEST' | SessionEnvironment
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
  risk_policy: RiskPolicy
  shared_capital: number | null
  created_at: string | null
  instances: ScenarioInstance[]
  runs?: ResearchRun[]
}

export type ExperimentStatus = 'empty' | 'not_run' | 'running' | 'partial' | 'done' | 'failed'

export interface Experiment {
  id: number
  name: string
  description: string | null
  objective: string | null
  created_at: string | null
}

export interface ExperimentListItem extends Experiment {
  scenarios: number
  runs: Partial<Record<RunStatus, number>>
  status: ExperimentStatus
}

export interface ExperimentDetail extends Experiment {
  status: ExperimentStatus
  scenarios: Scenario[]
}

export interface RunDetail extends ResearchRun {
  scenario: Scenario
  dataset: {
    id: number
    dataset_hash: string
    bar_count: number
    spec: Record<string, unknown>
    source: string | null
    symbol_count: number | null
    first_ts: number | null
    last_ts: number | null
  } | null
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
  risk_policy: RiskPolicy
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

/** Where a deployment's sessions execute; fixed when it is created. */
export type SessionEnvironment = 'SANDBOX' | 'SHADOW' | 'LIVE'

export const ENVIRONMENT_LABEL: Record<SessionEnvironment, string> = {
  SANDBOX: 'Sandbox',
  SHADOW: 'Shadow',
  LIVE: 'Live',
}

export interface Deployment {
  id: number
  scenario_id: number
  name: string
  status: 'ACTIVE' | 'STOPPED'
  schedule_start: string
  schedule_stop: string
  schedule_days: string[]
  stale_policy: 'PAUSE' | 'FAIL_SAFE'
  environment: SessionEnvironment
  capital_limit: number | null
  confirmed_at: string | null
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
  environment?: SessionEnvironment
  /** Whether the venue (sandbox or broker) answered the session's last order call. */
  venue?: { answering: boolean | null; since: number | null; error: string | null }
  market_data: { status: 'CONNECTING' | 'WAITING' | 'LIVE' | 'STALE'; last_tick_at: number | null }
  accept_new?: boolean
  books?: LiveBook[]
  open_orders?: { platform_order_id: string; symbol: string; net: number }[]
  /** The latest strategy answer that changed: its weights by symbol. */
  last_signal?: {
    instance: number
    weights: Record<string, number | null>
    bar_ts: number
  } | null
  last_order?: { symbol: string; net: number; bar_ts: number } | null
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
  environment: SessionEnvironment
  attribution: AttributionRow[] | null
  live_state: LiveState | null
  provenance: Record<string, unknown> | null
  events: SessionEvent[]
  /** The same scenario's sessions that day in other environments. */
  same_day: { id: number; environment: SessionEnvironment; status: SandboxStatus }[]
}

export interface DeploymentPayload {
  scenario_id: number
  name: string
  schedule_start: string
  schedule_stop: string
  schedule_days: string[]
  stale_policy: 'PAUSE' | 'FAIL_SAFE'
  environment: SessionEnvironment
  /** LIVE only: the most capital the deployment may hold, and its typed name. */
  capital_limit?: number
  confirm?: string
}

export interface LiveOverview {
  enabled: boolean
  allotted: number
  kill_switch: { engaged: boolean; reason: string | null; by: string | null; at: string | null }
  sessions: ResearchRun[]
}

export interface LiveReconciliation {
  checked_at: string
  error: string | null
  positions: {
    symbol: string
    platform: number
    broker: number | null
    difference: number | null
  }[]
  unsettled_orders: {
    session_id: number
    platform_order_id: string
    broker_order_id: string | null
    symbol: string
    quantity: number
    status: string
  }[]
}

export interface RunComparison {
  runs: {
    id: number
    environment: string
    status: RunStatus
    trading_date: string | null
    metrics: RunMetrics | null
  }[]
  orders: {
    bar_ts: number
    portfolio: number
    symbol: string
    first: { net: number; reference_price: number; fill_price: number | null } | null
    second: { net: number; reference_price: number; fill_price: number | null } | null
    same_decision: boolean
    price_difference: number | null
  }[]
  summary: {
    same_decisions: number
    different_decisions: number
    average_price_difference: number | null
  }
}

/** One entry of a run's ledger journal, in the PRD's field set (section 16). */
export interface LedgerEntry {
  seq: number
  kind: string
  timestamp: number | null
  at: string | null
  session_id: number
  experiment_id: number
  scenario_id: number
  strategy_instance_id: number | null
  strategy_id: number | null
  strategy_version_id: number | null
  config_version_id: number | null
  signal_id: number | null
  platform_order_id: string | null
  execution_order_id: string | null
  fill_id: number | null
  trade_id: number | null
  symbol: string | null
  side: 'BUY' | 'SELL' | null
  quantity: number | null
  requested_price: number | null
  fill_price: number | null
  fees: number | null
  slippage: number | null
  gross_pnl: number | null
  net_pnl: number | null
  detail: string | null
}

export interface LedgerFilters {
  kinds: string[]
  instance: number | null
  symbol: string
}

export interface ReconciliationCheck {
  name: string
  /** null: not applicable yet (the run is still going). */
  passed: boolean | null
  detail: string
  problems: string[]
}

export const LEDGER_KINDS = [
  'SIGNAL',
  'TARGET_POSITION',
  'SQUARE_OFF',
  'ORDER_INTENT',
  'ORDER_SUBMITTED',
  'ORDER_FILLED',
  'ORDER_REJECTED',
  'ORDER_UNFILLED',
  'FILL',
  'FEE',
  'SLIPPAGE',
  'POSITION_UPDATE',
  'REALIZED_PNL',
  'UNREALIZED_PNL',
  'SESSION_EVENT',
  'MARK',
] as const

function ledgerQuery(filters: LedgerFilters, cursor?: number): string {
  const params = new URLSearchParams()
  if (cursor) params.set('cursor', String(cursor))
  if (filters.kinds.length) params.set('kinds', filters.kinds.join(','))
  if (filters.instance !== null) params.set('instance', String(filters.instance))
  if (filters.symbol.trim()) params.set('symbol', filters.symbol.trim())
  const query = params.toString()
  return query ? `?${query}` : ''
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
  ledger: (id: number, filters: LedgerFilters) =>
    [...researchQueryKeys.run(id), 'ledger', filters] as const,
  reconciliation: (id: number) => [...researchQueryKeys.run(id), 'reconciliation'] as const,
  deployments: () => [...researchQueryKeys.all, 'deployments'] as const,
  deployment: (id: number) => [...researchQueryKeys.deployments(), id] as const,
  session: (id: number) => [...researchQueryKeys.all, 'sessions', id] as const,
  live: () => [...researchQueryKeys.all, 'live'] as const,
  compare: (a: number, b: number) => [...researchQueryKeys.all, 'compare', a, b] as const,
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

export async function createExperiment(
  name: string,
  description: string,
  objective = ''
): Promise<Experiment> {
  const data = await call(
    webClient.post<Envelope & { item: Experiment }>('/research/api/experiments', {
      name,
      description,
      objective,
    }),
    'Could not create the experiment'
  )
  return data.item
}

export const STRATEGY_STATUS_LABEL: Record<StrategyStatus, string> = {
  ready: 'Ready',
  deployed: 'Deployed',
  trading: 'Trading now',
  not_installed: 'Not installed',
}

export const EXPERIMENT_STATUS_LABEL: Record<ExperimentStatus, string> = {
  empty: 'No scenarios',
  not_run: 'Not run',
  running: 'Running',
  partial: 'Partly run',
  done: 'Done',
  failed: 'A run failed',
}

/** Backtest one strategy from its registry row; returns the queued (or matching) run. */
export async function backtestStrategy(
  strategyId: number,
  payload: { config_version_id?: number; start: string; end: string }
): Promise<ResearchRun & { created: boolean }> {
  const data = await call(
    webClient.post<Envelope & { item: ResearchRun & { created: boolean } }>(
      `/research/api/strategies/${strategyId}/backtest`,
      payload
    ),
    'Could not start the backtest'
  )
  return data.item
}

/** The one-strategy scenario a registry deployment runs. */
export async function strategyScenario(
  strategyId: number,
  payload: { config_version_id?: number } = {}
): Promise<Scenario> {
  const data = await call(
    webClient.post<Envelope & { item: Scenario }>(
      `/research/api/strategies/${strategyId}/scenario`,
      payload
    ),
    'Could not prepare the deployment'
  )
  return data.item
}

export async function controlStrategy(
  strategyId: number,
  action: 'pause' | 'stop'
): Promise<unknown[]> {
  const data = await call(
    webClient.post<Envelope & { item: { items: unknown[] } }>(
      `/research/api/strategies/${strategyId}/${action}`
    ),
    action === 'pause' ? 'Could not pause the strategy' : 'Could not stop the strategy'
  )
  return data.item.items
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

export async function getRunLedger(
  id: number,
  filters: LedgerFilters,
  cursor: number
): Promise<{ items: LedgerEntry[]; next_cursor: number | null }> {
  const data = await call(
    webClient.get<Envelope & { items: LedgerEntry[]; next_cursor: number | null }>(
      `/research/api/runs/${id}/ledger${ledgerQuery(filters, cursor)}`
    ),
    'Could not load the ledger'
  )
  return { items: data.items, next_cursor: data.next_cursor }
}

/** Where the browser downloads the whole filtered ledger as CSV. */
export function runLedgerCsvUrl(id: number, filters: LedgerFilters): string {
  return `/research/api/runs/${id}/ledger.csv${ledgerQuery(filters)}`
}

export async function getRunReconciliation(
  id: number
): Promise<{ checks: ReconciliationCheck[]; passed: boolean }> {
  const data = await call(
    webClient.get<Envelope & { item: { checks: ReconciliationCheck[]; passed: boolean } }>(
      `/research/api/runs/${id}/reconciliation`
    ),
    'Could not check the ledger'
  )
  return data.item
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
    'Could not deploy the scenario'
  )
  return data.item
}

export async function getLiveOverview(): Promise<LiveOverview> {
  const data = await call(
    webClient.get<Envelope & { item: LiveOverview }>('/research/api/live'),
    'Could not load Live'
  )
  return data.item
}

export async function setKillSwitch(
  engaged: boolean,
  reason = ''
): Promise<LiveOverview['kill_switch']> {
  const data = await call(
    webClient.post<Envelope & { item: LiveOverview['kill_switch'] }>('/research/api/live/kill', {
      engaged,
      reason,
    }),
    engaged ? 'Could not engage the kill switch' : 'Could not release the kill switch'
  )
  return data.item
}

export async function getLiveReconciliation(): Promise<LiveReconciliation> {
  const data = await call(
    webClient.get<Envelope & { item: LiveReconciliation }>('/research/api/live/reconciliation'),
    'Could not check against the broker'
  )
  return data.item
}

export async function setLiveApproval(versionId: number, approved: boolean): Promise<void> {
  await call(
    webClient.post<Envelope>(`/research/api/versions/${versionId}/live`, { approved }),
    'Could not change whether this version may trade live'
  )
}

export async function compareRuns(first: number, second: number): Promise<RunComparison> {
  const data = await call(
    webClient.get<Envelope & { item: RunComparison }>(
      `/research/api/runs/${first}/compare/${second}`
    ),
    'Could not compare the runs'
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
