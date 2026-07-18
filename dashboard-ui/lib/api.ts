import type { ApiPayload, HealthPayload } from './types'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:7070'

const TIMEOUT_MS = 15000

// Auth token: prefer the meta tag injected by the Python server (keeps the
// secret out of the JS bundle), then fall back to the build-time env var
// (set NEXT_PUBLIC_HERMX_TOKEN in .env.local for dev).
function authHeaders(): HeadersInit {
  const metaToken =
    typeof document !== 'undefined'
      ? document
          .querySelector<HTMLMetaElement>('meta[name="hermx-token"]')
          ?.content
      : undefined
  const token = metaToken || process.env.NEXT_PUBLIC_HERMX_TOKEN
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function fetchJson<T>(path: string): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)
  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, {
      headers: { Accept: 'application/json', ...authHeaders() },
      signal: controller.signal,
      cache: 'no-store',
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error(`Request to ${path} timed out after ${TIMEOUT_MS}ms`)
    }
    throw new Error(`Request to ${path} failed: ${(err as Error).message}`)
  } finally {
    clearTimeout(timer)
  }

  if (!res.ok) {
    throw new Error(`Request to ${path} failed: ${res.status} ${res.statusText}`)
  }

  try {
    return (await res.json()) as T
  } catch {
    throw new Error(`Request to ${path} returned invalid JSON`)
  }
}

export async function fetchApi(): Promise<ApiPayload> {
  return fetchJson<ApiPayload>('/api')
}

export async function fetchHealth(): Promise<HealthPayload> {
  return fetchJson<HealthPayload>('/health')
}

async function mutateJson(
  label: string,
  path: string,
  method: 'POST' | 'DELETE',
  body?: unknown,
): Promise<void> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)
  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        ...authHeaders(),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      cache: 'no-store',
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error(`${label} timed out`)
    }
    throw new Error(`${label} failed: ${(err as Error).message}`)
  } finally {
    clearTimeout(timer)
  }
  if (!res.ok) {
    let detail = ''
    try { detail = await res.text() } catch { /* ignore */ }
    throw new Error(`${label} ${res.status}: ${detail}`)
  }
}

export async function setStrategyMode(
  strategyId: string,
  mode: 'pause' | 'demo' | 'live' | 'clear',
): Promise<void> {
  return mutateJson(
    'setStrategyMode',
    `/api/control/strategy/${encodeURIComponent(strategyId)}`,
    'POST',
    { mode },
  )
}

/** Split control model: set the per-strategy RISK posture. "reduce" blocks
 * opens/reversals at the execution gate; closes always pass. Never touches
 * the account (execution_mode). */
export async function setStrategyRisk(
  strategyId: string,
  riskState: 'active' | 'reduce',
): Promise<void> {
  return mutateJson(
    'setStrategyRisk',
    `/api/control/strategy/${encodeURIComponent(strategyId)}`,
    'POST',
    { risk_state: riskState },
  )
}

/** Split control model: set the per-strategy ACCOUNT (demo sandbox vs live
 * venue). Never touches the risk posture. Live requires HERMX_LIVE_TRADING
 * server-side (403 otherwise). */
export async function setStrategyAccount(
  strategyId: string,
  executionMode: 'demo' | 'live',
): Promise<void> {
  return mutateJson(
    'setStrategyAccount',
    `/api/control/strategy/${encodeURIComponent(strategyId)}`,
    'POST',
    { execution_mode: executionMode },
  )
}

/** Enter the global reduce-only emergency state (closes always pass). */
export async function setTradingState(state: 'active' | 'reducing'): Promise<void> {
  return mutateJson('setTradingState', '/api/control/trading-state', 'POST', { state })
}

/** Restore normal trading (DELETE resets trading_state to "active"). */
export async function clearTradingState(): Promise<void> {
  return mutateJson('clearTradingState', '/api/control/trading-state', 'DELETE')
}
