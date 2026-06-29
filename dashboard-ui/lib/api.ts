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
