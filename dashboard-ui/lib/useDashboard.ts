'use client'

import { useState, useEffect, useCallback, useRef } from 'react'
import { fetchApi, fetchHealth } from './api'
import type { ApiPayload, HealthPayload } from './types'

const REFRESH_INTERVAL = Number(
  process.env.NEXT_PUBLIC_REFRESH_INTERVAL ?? 5000,
)

export interface DashboardState {
  data: ApiPayload | null
  health: HealthPayload | null
  loading: boolean
  error: string | null
  lastUpdated: Date | null
  refresh: () => void
}

export function useDashboard(): DashboardState {
  const [data, setData] = useState<ApiPayload | null>(null)
  const [health, setHealth] = useState<HealthPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const timer = useRef<ReturnType<typeof setInterval> | null>(null)
  // Guards against setting state after unmount or out-of-order responses.
  const mounted = useRef(true)

  const load = useCallback(async () => {
    try {
      const [api, hp] = await Promise.all([fetchApi(), fetchHealth()])
      if (!mounted.current) return
      setData(api)
      setHealth(hp)
      setError(null)
      setLastUpdated(new Date())
    } catch (err) {
      if (!mounted.current) return
      // Keep the last good data; surface the error string only.
      setError((err as Error).message)
    } finally {
      if (mounted.current) setLoading(false)
    }
  }, [])

  const stop = useCallback(() => {
    if (timer.current !== null) {
      clearInterval(timer.current)
      timer.current = null
    }
  }, [])

  const start = useCallback(() => {
    stop()
    timer.current = setInterval(load, REFRESH_INTERVAL)
  }, [load, stop])

  // Manual trigger: fetch now and restart the interval so the next tick is a
  // full interval away from this fetch.
  const refresh = useCallback(() => {
    void load()
    start()
  }, [load, start])

  useEffect(() => {
    mounted.current = true

    const onVisibility = () => {
      if (document.hidden) {
        stop()
      } else {
        void load()
        start()
      }
    }

    // Initial fetch + polling, unless the tab opens hidden.
    if (!document.hidden) {
      void load()
      start()
    } else {
      setLoading(false)
    }

    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      mounted.current = false
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [load, start, stop])

  return { data, health, loading, error, lastUpdated, refresh }
}
