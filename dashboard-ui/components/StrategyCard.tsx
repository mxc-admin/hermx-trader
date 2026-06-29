'use client'
import type { CSSProperties } from 'react'
import { useState, useCallback } from 'react'
import type { Strategy, LivePosition } from '../lib/types'
import { money, num, sideColor, sideKind } from '../lib/format'
import { Badge } from './Badge'
import { setStrategyMode } from '../lib/api'

interface StrategyCardProps {
  strategy: Strategy
  position?: LivePosition
  alertCount: number
  liveEnabled: boolean
  onModeChange?: () => void
}

const MODES = ['shadow', 'demo', 'live'] as const
type Mode = typeof MODES[number]

function ModePill({
  current,
  liveEnabled,
  onSelect,
  pending,
}: {
  current: Mode
  liveEnabled: boolean
  onSelect: (m: Mode) => void
  pending: boolean
}) {
  const modeLabel: Record<Mode, string> = { shadow: 'Shadow', demo: 'Demo', live: 'Live' }
  const modeColor: Record<Mode, string> = {
    shadow: 'var(--text-muted)',
    demo: 'var(--text-primary)',
    live: 'var(--positive)',
  }

  return (
    <div
      style={{
        display: 'inline-flex',
        border: '1px solid var(--border-dim)',
        borderRadius: 6,
        overflow: 'hidden',
        opacity: pending ? 0.6 : 1,
        pointerEvents: pending ? 'none' : 'auto',
        flexShrink: 0,
      }}
    >
      {MODES.map((m) => {
        const isActive = current === m
        const isLocked = m === 'live' && !liveEnabled
        return (
          <button
            key={m}
            title={isLocked ? 'Requires HERMX_LIVE_TRADING=true' : undefined}
            disabled={isLocked || pending}
            onClick={() => !isLocked && onSelect(m)}
            style={{
              padding: '3px 10px',
              fontSize: 11,
              fontWeight: isActive ? 700 : 400,
              background: isActive ? 'var(--bg-hover, rgba(255,255,255,0.08))' : 'transparent',
              color: isActive ? modeColor[m] : isLocked ? 'var(--text-muted)' : 'var(--text-secondary)',
              border: 'none',
              borderLeft: m !== 'shadow' ? '1px solid var(--border-dim)' : 'none',
              cursor: isLocked ? 'not-allowed' : 'pointer',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              lineHeight: 1,
              transition: 'background 0.15s',
            }}
          >
            {isLocked && <span style={{ fontSize: 10 }}>🔒</span>}
            {modeLabel[m]}
          </button>
        )
      })}
    </div>
  )
}

export function StrategyCard({ strategy, position, alertCount, liveEnabled, onModeChange }: StrategyCardProps) {
  const sym = strategy.asset ?? ''
  const side = (position?.side ?? 'FLAT').toUpperCase()
  const isLive = side !== 'FLAT'

  const effectiveMode = (strategy.effective_mode ?? 'demo') as Mode
  const [pending, setPending] = useState(false)
  const [modeError, setModeError] = useState<string | null>(null)

  const budget = strategy.capital?.budget_usd ?? strategy.budget_usd ?? 0
  const realized = position?.realized_pnl ?? 0
  const upl = position?.upl ?? 0
  const equityNow = budget + realized + upl

  const logo = `https://assets.coincap.io/assets/icons/${sym
    .replace(/USDT?$/i, '')
    .toLowerCase()}@2x.png`

  const uplColor =
    upl > 0 ? 'var(--positive)' : upl < 0 ? 'var(--negative)' : 'var(--text-muted)'

  const handleModeChange = useCallback(
    async (mode: Mode) => {
      if (!strategy.strategy_id) return
      setPending(true)
      setModeError(null)
      try {
        await setStrategyMode(strategy.strategy_id, mode)
        onModeChange?.()
      } catch (e) {
        setModeError((e as Error).message)
      } finally {
        setPending(false)
      }
    },
    [strategy.strategy_id, onModeChange],
  )

  const cardStyle: CSSProperties = {
    background: 'var(--bg-panel)',
    border: '1px solid var(--border-dim)',
    borderRadius: 6,
    borderLeft: isLive ? `3px solid ${sideColor(side)}` : '1px solid var(--border-dim)',
    padding: 16,
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
  }

  return (
    <section style={cardStyle}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={logo}
            alt={`${sym} logo`}
            width={28}
            height={28}
            loading="lazy"
            style={{ borderRadius: '50%', flexShrink: 0 }}
            onError={(e) => {
              e.currentTarget.style.visibility = 'hidden'
            }}
          />
          <div style={{ minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <h3 style={{ fontSize: 15, fontWeight: 600, color: 'var(--text-primary)' }}>{sym}</h3>
              <Badge label={strategy.timeframe ?? '-'} kind="neutral" />
            </div>
            <p style={{ fontSize: 12, color: 'var(--text-secondary)', margin: 0 }}>
              {strategy.name ?? sym}
            </p>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          <ModePill
            current={effectiveMode}
            liveEnabled={liveEnabled}
            onSelect={handleModeChange}
            pending={pending}
          />
          {isLive ? (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              <Badge label={side} kind={sideKind(side)} />
              <span className="live-dot active" />
            </span>
          ) : (
            <Badge label="FLAT" kind="muted" />
          )}
        </div>
      </div>

      {/* Mode error (transient) */}
      {modeError && (
        <p style={{ fontSize: 11, color: 'var(--negative)', margin: 0 }}>{modeError}</p>
      )}

      {/* Config strip */}
      <div>
        <span className="metric-label">Strategy config</span>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 6 }}>
          <Badge label={strategy.indicator ?? '-'} kind="neutral" />
          <Badge label={`${strategy.leverage ?? '-'}x`} kind="neutral" />
          <Badge label={strategy.margin_mode ?? '-'} kind="neutral" />
          <Badge label={strategy.instrument?.type ?? '-'} kind="neutral" />
          <Badge label={strategy.instrument?.exchange ?? '-'} kind="good" />
        </div>
      </div>

      {/* Metrics */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(3, minmax(0, 1fr))',
          gap: 12,
        }}
      >
        <Metric label="Budget" value={money(budget, 0)} />
        <Metric label="Equity now" value={money(equityNow, 2)} />
        <Metric
          label="UPnL"
          value={`${upl > 0 ? '+' : ''}${money(upl, 2)}`}
          color={uplColor}
        />
        <Metric label="Mark price" value={num(position?.last, 4)} />
        <Metric label="Alerts" value={String(alertCount)} />
      </div>
    </section>
  )
}

function Metric({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
      <span className="metric-label">{label}</span>
      <b
        style={{
          fontFamily: 'var(--font-mono), monospace',
          fontSize: 16,
          color: color ?? 'var(--text-primary)',
        }}
      >
        {value}
      </b>
    </div>
  )
}

export default StrategyCard
