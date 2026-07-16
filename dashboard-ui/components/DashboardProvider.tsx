'use client'
import { createContext, useContext, useState } from 'react'
import { useDashboard, DashboardState } from '../lib/useDashboard'

export interface DashboardContextState extends DashboardState {
  /** Selected strategy_id, or null for all — filters positions/events/alerts. */
  strategyFilter: string | null
  setStrategyFilter: (sid: string | null) => void
}

const Ctx = createContext<DashboardContextState | null>(null)

export function DashboardProvider({ children }: { children: React.ReactNode }) {
  const state = useDashboard()
  const [strategyFilter, setStrategyFilter] = useState<string | null>(null)
  return (
    <Ctx.Provider value={{ ...state, strategyFilter, setStrategyFilter }}>
      {children}
    </Ctx.Provider>
  )
}

export function useDashboardContext(): DashboardContextState {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useDashboardContext must be used inside DashboardProvider')
  return ctx
}
