'use client'
import type { CSSProperties } from 'react'
import { useState, useCallback } from 'react'
import { ShieldAlert, ShieldCheck, ShieldOff } from 'lucide-react'
import { useDashboardContext } from './DashboardProvider'
import { deriveSystemStatus } from '../lib/systemStatus'
import { setTradingState, clearTradingState } from '../lib/api'

// Top-of-page strip reflecting the 2-control arming model (execution_mode per
// strategy + the global HERMX_LIVE_TRADING kill switch). Mirrors health.arm
// from src/dashboard.py:health_payload().
export function ArmingBanner() {
  const { health, data, refresh } = useDashboardContext()
  const [pending, setPending] = useState(false)
  const [ctrlError, setCtrlError] = useState<string | null>(null)

  const reducing = data?.trading_state === 'reducing'

  const toggleTradingState = useCallback(async () => {
    if (reducing) {
      if (!window.confirm('Restore ACTIVE trading? New entries will be allowed again.')) return
    } else if (
      !window.confirm(
        'Enter REDUCE-ONLY mode? New entries and reversals are blocked; closes always pass.',
      )
    ) {
      return
    }
    setPending(true)
    setCtrlError(null)
    try {
      if (reducing) await clearTradingState()
      else await setTradingState('reducing')
      refresh()
    } catch (e) {
      setCtrlError((e as Error).message)
    } finally {
      setPending(false)
    }
  }, [reducing, refresh])

  // Loading / no data: render nothing rather than a misleading "disarmed".
  if (!health) return null

  const status = deriveSystemStatus(health.arm)
  const live = status.liveStrategies
  const demo = status.demoStrategies

  let tint: string
  let color: string
  let Icon: typeof ShieldAlert
  let message: string

  if (status.kind === 'armed') {
    color = 'var(--negative)'
    tint = 'color-mix(in srgb, var(--negative) 14%, transparent)'
    Icon = ShieldAlert
    message = `LIVE TRADING ARMED — ${live} live ${live === 1 ? 'strategy' : 'strategies'}`
  } else if (status.kind === 'demo') {
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

  const chipColor = reducing ? 'var(--warning)' : 'var(--text-muted)'
  const chipStyle: CSSProperties = {
    marginLeft: 'auto',
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    padding: '4px 10px',
    borderRadius: 4,
    background: reducing
      ? 'color-mix(in srgb, var(--warning) 14%, transparent)'
      : 'transparent',
    color: chipColor,
    border: `1px solid color-mix(in srgb, ${chipColor} 40%, transparent)`,
    font: 'inherit',
    letterSpacing: 'inherit',
    textTransform: 'inherit',
    cursor: pending ? 'wait' : 'pointer',
    opacity: pending ? 0.6 : 1,
  }

  return (
    <div style={style} role="status" aria-live="polite">
      <Icon size={16} aria-hidden />
      <span>{message}</span>
      {ctrlError && (
        <span style={{ color: 'var(--negative)', fontWeight: 400 }}>{ctrlError}</span>
      )}
      {data && (
        <button
          type="button"
          onClick={toggleTradingState}
          disabled={pending}
          title={
            reducing
              ? 'Click to restore active trading'
              : 'Click to block new entries (closes always pass)'
          }
          style={chipStyle}
        >
          {reducing ? 'REDUCE-ONLY — closes only' : 'ACTIVE'}
        </button>
      )}
    </div>
  )
}

export default ArmingBanner
