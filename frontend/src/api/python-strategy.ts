import type {
  EnvironmentVariables,
  LogContent,
  LogFile,
  MasterContractStatus,
  PythonStrategy,
  PythonStrategyContent,
  ScheduleConfig,
  StrategyAccount,
  StrategyExchangeList,
  StrategyMode,
  SymbolList,
} from '@/types/python-strategy'
import type { ApiResponse } from '@/types/trading'
import { webClient } from './client'

export const pythonStrategyApi = {
  /**
   * Get the exchange options for the strategy selector.
   *
   * Session windows come from the market calendar DB, never from a constant
   * in the frontend, so an exchange timing change reaches the UI on its own.
   */
  getExchanges: async (): Promise<StrategyExchangeList> => {
    const response = await webClient.get<Partial<StrategyExchangeList>>('/python/api/exchanges')
    return { exchanges: response.data.exchanges || [], default: response.data.default || '' }
  },

  /**
   * Get all Python strategies
   */
  getStrategies: async (): Promise<PythonStrategy[]> => {
    const response = await webClient.get<{ strategies: PythonStrategy[] }>('/python/api/strategies')
    return response.data.strategies || []
  },

  /**
   * Get a single strategy
   */
  getStrategy: async (strategyId: string): Promise<PythonStrategy> => {
    const response = await webClient.get<{ strategy: PythonStrategy }>(
      `/python/api/strategy/${strategyId}`
    )
    return response.data.strategy
  },

  /**
   * Get strategy content for editing
   */
  getStrategyContent: async (strategyId: string): Promise<PythonStrategyContent> => {
    const response = await webClient.get<PythonStrategyContent>(
      `/python/api/strategy/${strategyId}/content`
    )
    return response.data
  },

  /**
   * Upload a new strategy with mandatory schedule
   */
  uploadStrategy: async (
    name: string,
    file: File,
    schedule: {
      start_time: string
      /** Null runs a crypto strategy 24/7. */
      stop_time: string | null
      days: string[]
      exchange?: string
    }
  ): Promise<ApiResponse<{ strategy_id: string }>> => {
    const formData = new FormData()
    formData.append('strategy_name', name)
    formData.append('strategy_file', file)
    formData.append('exchange', schedule.exchange || 'NSE')
    // Add schedule fields
    formData.append('schedule_start', schedule.start_time)
    // Left out for a 24/7 crypto strategy: FormData would send null as the
    // text "null".
    if (schedule.stop_time) formData.append('schedule_stop', schedule.stop_time)
    formData.append('schedule_days', JSON.stringify(schedule.days))

    const response = await webClient.post<ApiResponse<{ strategy_id: string }>>(
      '/python/new',
      formData,
      {
        headers: {
          'Content-Type': 'multipart/form-data',
        },
      }
    )
    return response.data
  },

  /**
   * Save strategy content
   */
  saveStrategy: async (strategyId: string, content: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/save/${strategyId}`, {
      content,
    })
    return response.data
  },

  /**
   * Export strategy file
   */
  exportStrategy: async (
    strategyId: string,
    version: 'saved' | 'current' = 'saved'
  ): Promise<Blob> => {
    const response = await webClient.get(`/python/export/${strategyId}?version=${version}`, {
      responseType: 'blob',
    })
    return response.data
  },

  /**
   * Delete a strategy
   */
  deleteStrategy: async (strategyId: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/delete/${strategyId}`)
    return response.data
  },

  /**
   * Start a strategy
   */
  startStrategy: async (strategyId: string): Promise<ApiResponse<{ process_id: number }>> => {
    const response = await webClient.post<ApiResponse<{ process_id: number }>>(
      `/python/start/${strategyId}`
    )
    return response.data
  },

  /**
   * Stop a strategy
   */
  stopStrategy: async (strategyId: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/stop/${strategyId}`)
    return response.data
  },

  /**
   * Clear error state
   */
  clearError: async (strategyId: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/clear-error/${strategyId}`)
    return response.data
  },

  /**
   * Schedule a strategy
   */
  scheduleStrategy: async (
    strategyId: string,
    config: ScheduleConfig
  ): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(
      `/python/schedule/${strategyId}`,
      config
    )
    return response.data
  },

  /**
   * Unschedule a strategy
   */
  setMode: async (strategyId: string, mode: StrategyMode): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/mode/${strategyId}`, {
      mode,
    })
    return response.data
  },
  listSymbolLists: async (): Promise<SymbolList[]> => {
    const response = await webClient.get<{ lists: SymbolList[] }>('/python/api/symbol-lists')
    return response.data.lists
  },
  /** Create a list, or replace the one ``id`` names. ``symbols`` is pasted text. */
  saveSymbolList: async (list: {
    id?: string
    name: string
    exchange: string
    symbols: string
  }): Promise<SymbolList> => {
    const body = { name: list.name, exchange: list.exchange, symbols: list.symbols }
    const response = list.id
      ? await webClient.put<{ list: SymbolList }>(`/python/api/symbol-lists/${list.id}`, body)
      : await webClient.post<{ list: SymbolList }>('/python/api/symbol-lists', body)
    return response.data.list
  },
  deleteSymbolList: async (id: string): Promise<void> => {
    await webClient.delete(`/python/api/symbol-lists/${id}`)
  },
  setSymbolList: async (strategyId: string, listId: string | null): Promise<void> => {
    await webClient.post(`/python/symbol-list/${strategyId}`, { list_id: listId })
  },
  setCapital: async (strategyId: string, capital: number): Promise<number> => {
    const response = await webClient.post<{ capital: number }>(`/python/capital/${strategyId}`, {
      capital,
    })
    return response.data.capital
  },
  /** Give a strategy a Sandbox account of its own, opened with ``capital``. */
  bindAccount: async (
    strategyId: string,
    label: string,
    capital: number
  ): Promise<StrategyAccount> => {
    const response = await webClient.post<{ account: StrategyAccount }>(
      `/python/account/${strategyId}`,
      { label, capital }
    )
    return response.data.account
  },
  /** Add money to its account, or take it out with a negative amount. */
  fundAccount: async (strategyId: string, amount: number): Promise<StrategyAccount> => {
    const response = await webClient.post<{ account: StrategyAccount }>(
      `/python/account/${strategyId}/funds`,
      { amount }
    )
    return response.data.account
  },
  closeAccount: async (strategyId: string): Promise<void> => {
    await webClient.post(`/python/account/${strategyId}/close`)
  },
  /** Replace a strategy's own settings; an empty set clears them. */
  setSettings: async (
    strategyId: string,
    env: Record<string, string>
  ): Promise<Record<string, string>> => {
    const response = await webClient.post<{ env: Record<string, string> }>(
      `/python/settings/${strategyId}`,
      { env }
    )
    return response.data.env
  },
  unscheduleStrategy: async (strategyId: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/unschedule/${strategyId}`)
    return response.data
  },

  /**
   * Get log files for a strategy
   */
  getLogFiles: async (strategyId: string): Promise<LogFile[]> => {
    const response = await webClient.get<{ logs: LogFile[] }>(`/python/api/logs/${strategyId}`)
    return response.data.logs || []
  },

  /**
   * Get log file content
   */
  getLogContent: async (strategyId: string, logName: string): Promise<LogContent> => {
    const response = await webClient.get<LogContent>(`/python/api/logs/${strategyId}/${logName}`)
    return response.data
  },

  /**
   * Clear all logs for a strategy
   */
  clearLogs: async (strategyId: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/logs/${strategyId}/clear`)
    return response.data
  },

  /**
   * Get environment variables
   */
  getEnvVariables: async (strategyId: string): Promise<EnvironmentVariables> => {
    const response = await webClient.get<EnvironmentVariables>(`/python/env/${strategyId}`)
    return response.data
  },

  /**
   * Save environment variables
   */
  saveEnvVariables: async (
    strategyId: string,
    variables: EnvironmentVariables
  ): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/env/${strategyId}`, variables)
    return response.data
  },

  /**
   * Get master contract status
   */
  getMasterContractStatus: async (): Promise<MasterContractStatus> => {
    const response = await webClient.get<MasterContractStatus>('/python/status')
    return response.data
  },

  /**
   * Check and start pending strategies (after master contract download)
   */
  checkAndStartPending: async (): Promise<ApiResponse<{ started: number }>> => {
    const response =
      await webClient.post<ApiResponse<{ started: number }>>('/python/check-contracts')
    return response.data
  },
}
