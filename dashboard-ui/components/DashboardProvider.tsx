'use client'
import { createContext, useContext, useState } from 'react'
import { useDashboard, DashboardState } from '../lib/useDashboard'

/** A clicked position: its order ids drive an EXACT cl_ord_id event filter. */
export interface PositionSelection {
  /** Stable identity of the clicked row (click again to clear). */
  key: string
  /** Human label for the filter chip, e.g. "BTCUSDT open · alpha". */
  label: string
  /** Exact cl_ord_ids of the episode's open+close orders. Empty → zero events. */
  clOrdIds: string[]
}

export interface DashboardContextState extends DashboardState {
  /** Selected strategy_id, or null for all — filters positions/events/alerts. */
  strategyFilter: string | null
  setStrategyFilter: (sid: string | null) => void
  /** Selected position, or null — filters execution events by exact cl_ord_id. */
  positionFilter: PositionSelection | null
  setPositionFilter: (sel: PositionSelection | null) => void
}

const Ctx = createContext<DashboardContextState | null>(null)

export function DashboardProvider({ children }: { children: React.ReactNode }) {
  const state = useDashboard()
  const [strategyFilter, setStrategyFilter] = useState<string | null>(null)
  const [positionFilter, setPositionFilter] = useState<PositionSelection | null>(null)
  return (
    <Ctx.Provider
      value={{ ...state, strategyFilter, setStrategyFilter, positionFilter, setPositionFilter }}
    >
      {children}
    </Ctx.Provider>
  )
}

export function useDashboardContext(): DashboardContextState {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useDashboardContext must be used inside DashboardProvider')
  return ctx
}
