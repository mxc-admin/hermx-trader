'use client'
import type { CSSProperties } from 'react'
import { useState, useCallback } from 'react'
import type { Strategy, LivePosition } from '../lib/types'
import { money, num, pct, sideColor, sideKind } from '../lib/format'
import { Badge } from './Badge'
import { setStrategyMode } from '../lib/api'

interface StrategyCardProps {
  strategy: Strategy
  position?: LivePosition
  alertCount: number
  liveEnabled: boolean
  onModeChange?: () => void
}

const MODES = ['pause', 'demo', 'live'] as const
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
  const modeLabel: Record<Mode, string> = { pause: 'Pause', demo: 'Demo', live: 'Live' }
  const modeColor: Record<Mode, string> = {
    pause: 'var(--text-muted)',
    demo: 'var(--text-primary)',
    live: 'var(--positive)',
  }

  return (
    <div
      role="group"
      aria-label="Trading mode"
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
            aria-label={
              isLocked ? 'Live mode — requires HERMX_LIVE_TRADING=true' : undefined
            }
            aria-pressed={isActive}
            disabled={isLocked || pending}
            onClick={() => !isLocked && onSelect(m)}
            style={{
              padding: '8px 14px',
              fontSize: 13,
              fontWeight: isActive ? 700 : 400,
              background: isActive ? 'var(--bg-hover, rgba(255,255,255,0.08))' : 'transparent',
              color: isActive ? modeColor[m] : isLocked ? 'var(--text-muted)' : 'var(--text-secondary)',
              border: 'none',
              borderLeft: m !== 'pause' ? '1px solid var(--border-dim)' : 'none',
              cursor: isLocked ? 'not-allowed' : 'pointer',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              lineHeight: 1,
              transition: 'background 0.15s',
            }}
          >
            {isLocked && <span aria-hidden="true" style={{ fontSize: 12 }}>🔒</span>}
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

  // Durable per-strategy P&L (Phase 4). Prefer it over the local position-derived
  // calc so the Equity tile and the Performance % read from one source and never
  // disagree in production.
  const pnl = strategy.strategy_pnl
  const closes = pnl?.trade_count ?? 0
  // Equity now: read the durable value; fall back to the local formula only when
  // strategy_pnl is entirely absent.
  const equityNowDisplay = pnl ? pnl.equity_now_usd : equityNow
  // UPnL %: numerator is the displayed dollar upl; denominator is capital-at-risk
  // (equity_now - upl = budget + closed_net), NOT seed budget — reinvest=true
  // overstates seed by ~2x. Fall back to local budget only when strategy_pnl is
  // absent; suppress (null) when the denominator is <= 0 so we never emit NaN/Inf.
  const capitalAtRisk = pnl ? (pnl.equity_now_usd ?? 0) - (pnl.upl ?? 0) : budget
  const uplPct = capitalAtRisk > 0 ? (upl / capitalAtRisk) * 100 : null
  // Performance: growth of durable equity over seed budget, sourced from
  // strategy_pnl (same source as the Equity tile). Guard divide-by-zero.
  const perfPct =
    pnl && (pnl.budget_usd ?? 0) > 0 && pnl.equity_now_usd !== undefined
      ? ((pnl.equity_now_usd - (pnl.budget_usd ?? 0)) / (pnl.budget_usd ?? 0)) * 100
      : null
  const perfColor =
    perfPct === null
      ? undefined
      : perfPct > 0
        ? 'var(--positive)'
        : perfPct < 0
          ? 'var(--negative)'
          : 'var(--text-muted)'

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

  // Card shell uses the shared .panel class (background + border + radius).
  // Only the dynamic left-border accent and layout metrics stay inline.
  const cardStyle: CSSProperties = {
    borderLeft: isLive ? `3px solid ${sideColor(side)}` : '1px solid var(--border-dim)',
    padding: 16,
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
  }

  return (
    <section className="panel" style={cardStyle}>
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
        <Metric label="Equity now" value={money(equityNowDisplay, 2)} />
        <Metric
          label="UPnL"
          value={`${upl > 0 ? '+' : ''}${money(upl, 2)}${
            uplPct !== null ? ` (${pct(uplPct)})` : ''
          }`}
          color={uplColor}
        />
        <Metric label="Mark price" value={num(position?.last, 4)} />
        <Metric label="Alerts | Closes" value={`${alertCount} | ${closes}`} />
        <Metric label="Performance" value={pct(perfPct)} color={perfColor} />
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
