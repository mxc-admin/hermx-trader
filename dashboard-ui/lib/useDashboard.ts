'use client'

import { useState, useEffect, useCallback, useRef } from 'react'
import { fetchApi, fetchHealth } from './api'
import type { ApiPayload, HealthPayload } from './types'

const REFRESH_INTERVAL = Number(
  process.env.NEXT_PUBLIC_REFRESH_INTERVAL ?? 10000,
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

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mounted = useRef(true)

  const clearTimer = useCallback(() => {
    if (timer.current !== null) {
      clearTimeout(timer.current)
      timer.current = null
    }
  }, [])

  // Uses setTimeout (not setInterval) so the next poll only starts after the
  // current fetch fully completes — prevents overlapping requests on slow responses.
  const loadOnce = useCallback(async (reschedule: boolean) => {
    try {
      const [api, hp] = await Promise.all([fetchApi(), fetchHealth()])
      if (!mounted.current) return
      setData(api)
      setHealth(hp)
      setError(null)
      setLastUpdated(new Date())
    } catch (err) {
      if (!mounted.current) return
      // Keep the last good data visible; only update the error string.
      setError((err as Error).message)
    } finally {
      if (mounted.current) setLoading(false)
    }
    if (reschedule && mounted.current) {
      clearTimer()
      timer.current = setTimeout(() => { void loadOnce(true) }, REFRESH_INTERVAL)
    }
  }, [clearTimer]) // eslint-disable-line react-hooks/exhaustive-deps

  const refresh = useCallback(() => {
    clearTimer()
    void loadOnce(true)
  }, [clearTimer, loadOnce])

  useEffect(() => {
    mounted.current = true

    const onVisibility = () => {
      if (document.hidden) {
        clearTimer()
      } else {
        void loadOnce(true)
      }
    }

    if (!document.hidden) {
      void loadOnce(true)
    } else {
      setLoading(false)
    }

    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      mounted.current = false
      clearTimer()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [loadOnce, clearTimer])

  return { data, health, loading, error, lastUpdated, refresh }
}
