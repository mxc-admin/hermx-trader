'use client'
import { createContext, useContext } from 'react'
import { useDashboard, DashboardState } from '../lib/useDashboard'

const Ctx = createContext<DashboardState | null>(null)

export function DashboardProvider({ children }: { children: React.ReactNode }) {
  const state = useDashboard()
  return <Ctx.Provider value={state}>{children}</Ctx.Provider>
}

export function useDashboardContext(): DashboardState {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useDashboardContext must be used inside DashboardProvider')
  return ctx
}
