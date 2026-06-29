'use client'
import type { CSSProperties } from 'react'
import { ShieldAlert, ShieldCheck, ShieldOff } from 'lucide-react'
import { useDashboardContext } from './DashboardProvider'

// Top-of-page strip reflecting the 2-control arming model (execution_mode per
// strategy + the global HERMX_LIVE_TRADING kill switch). Mirrors health.arm
// from src/dashboard.py:health_payload().
export function ArmingBanner() {
  const { health } = useDashboardContext()
  // Loading / no data: render nothing rather than a misleading "disarmed".
  if (!health) return null

  const arm = health.arm ?? {}
  const live = arm.live_strategies ?? 0
  const demo = arm.demo_strategies ?? 0
  const armed = !!arm.armed

  let tint: string
  let color: string
  let Icon: typeof ShieldAlert
  let message: string

  if (armed) {
    color = 'var(--negative)'
    tint = 'color-mix(in srgb, var(--negative) 14%, transparent)'
    Icon = ShieldAlert
    message = `LIVE TRADING ARMED — ${live} live ${live === 1 ? 'strategy' : 'strategies'}`
  } else if (live + demo > 0) {
    color = 'var(--warning)'
    tint = 'color-mix(in srgb, var(--warning) 14%, transparent)'
    Icon = ShieldCheck
    message = `DEMO MODE — ${demo} ${demo === 1 ? 'strategy' : 'strategies'}, kill switch active`
  } else {
    color = 'var(--text-muted)'
    tint = 'var(--bg-panel-raised)'
    Icon = ShieldOff
    message = 'System disarmed'
  }

  const style: CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    width: '100%',
    height: 40,
    padding: '0 16px',
    background: tint,
    color,
    borderBottom: `1px solid color-mix(in srgb, ${color} 30%, transparent)`,
    fontFamily: 'var(--font-mono), monospace',
    fontSize: 12,
    fontWeight: 600,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
  }

  return (
    <div style={style} role="status">
      <Icon size={16} aria-hidden />
      <span>{message}</span>
    </div>
  )
}

export default ArmingBanner
