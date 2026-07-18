'use client'
import type { CSSProperties } from 'react'
import { useState, useCallback } from 'react'
import type { Strategy, LivePosition, PnlPoint, StrategyOverride } from '../lib/types'
import { age, money, num, pct, sideColor, sideKind } from '../lib/format'
import { Badge } from './Badge'
import { setStrategyAccount, setStrategyRisk } from '../lib/api'

interface StrategyCardProps {
  strategy: Strategy
  position?: LivePosition
  liveEnabled: boolean
  override?: StrategyOverride
  onModeChange?: () => void
}

// Split control model: two INDEPENDENT per-strategy axes. Risk (active|reduce)
// blocks opens/reversals at the execution gate — closes always pass — and never
// rewrites the account; Account (demo|live) selects sandbox vs real venue.
type RiskState = 'active' | 'reduce'
type Account = 'demo' | 'live'

interface SegmentedOption<T extends string> {
  value: T
  label: string
  color: string
  locked?: boolean
  lockLabel?: string
}

function SegmentedControl<T extends string>({
  groupLabel,
  options,
  current,
  onSelect,
  pending,
}: {
  groupLabel: string
  options: SegmentedOption<T>[]
  current: T
  onSelect: (v: T) => void
  pending: boolean
}) {
  return (
    <div
      role="group"
      aria-label={groupLabel}
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
      {options.map((opt, i) => {
        const isActive = current === opt.value
        return (
          <button
            key={opt.value}
            title={opt.locked ? 'Requires HERMX_LIVE_TRADING=true' : undefined}
            aria-label={opt.locked ? opt.lockLabel : undefined}
            aria-pressed={isActive}
            disabled={opt.locked || pending}
            onClick={() => !opt.locked && onSelect(opt.value)}
            style={{
              padding: '8px 14px',
              fontSize: 13,
              fontWeight: isActive ? 700 : 400,
              background: isActive ? 'var(--bg-hover, rgba(255,255,255,0.08))' : 'transparent',
              color: isActive ? opt.color : opt.locked ? 'var(--text-muted)' : 'var(--text-secondary)',
              border: 'none',
              borderLeft: i !== 0 ? '1px solid var(--border-dim)' : 'none',
              cursor: opt.locked ? 'not-allowed' : 'pointer',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              lineHeight: 1,
              transition: 'background 0.15s',
            }}
          >
            {opt.locked && <span aria-hidden="true" style={{ fontSize: 12 }}>🔒</span>}
            {opt.label}
          </button>
        )
      })}
    </div>
  )
}

export function StrategyCard({ strategy, position, liveEnabled, override, onModeChange }: StrategyCardProps) {
  const sym = strategy.asset ?? ''
  const side = (position?.side ?? 'FLAT').toUpperCase()
  const isLive = side !== 'FLAT'

  const effectiveMode = strategy.effective_mode ?? 'demo'
  // Risk posture: the override's risk_state; legacy "pause" display maps to reduce.
  const riskState: RiskState =
    override?.risk_state === 'reduce' || (!override?.risk_state && effectiveMode === 'pause')
      ? 'reduce'
      : 'active'
  // Account: execution_mode-honest (okx_account_source), never the "pause" display.
  const account: Account =
    (override?.execution_mode ?? strategy.okx_account_source ?? strategy.execution_mode) === 'live'
      ? 'live'
      : 'demo'
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

  const submitControl = useCallback(
    async (mutate: () => Promise<void>) => {
      setPending(true)
      setModeError(null)
      try {
        await mutate()
        onModeChange?.()
      } catch (e) {
        setModeError((e as Error).message)
      } finally {
        setPending(false)
      }
    },
    [onModeChange],
  )

  const handleRiskChange = useCallback(
    (next: RiskState) => {
      if (!strategy.strategy_id || next === riskState) return
      if (
        next === 'reduce' &&
        isLive &&
        !window.confirm(
          `${sym} has an open position. Reduce blocks new orders but does NOT close it (closes still pass). Continue?`,
        )
      ) {
        return
      }
      void submitControl(() => setStrategyRisk(strategy.strategy_id!, next))
    },
    [strategy.strategy_id, riskState, isLive, sym, submitControl],
  )

  const handleAccountChange = useCallback(
    (next: Account) => {
      if (!strategy.strategy_id || next === account) return
      if (
        next === 'live' &&
        !window.confirm(`Switch ${sym} to the LIVE account? Real-money orders will be submitted.`)
      ) {
        return
      }
      // An account flip never moves an existing position between venues.
      if (
        isLive &&
        !window.confirm(
          `${sym} has an open position on the ${account.toUpperCase()} account. Switching to ${next.toUpperCase()} does NOT move or close it. Continue?`,
        )
      ) {
        return
      }
      void submitControl(() => setStrategyAccount(strategy.strategy_id!, next))
    },
    [strategy.strategy_id, account, isLive, sym, submitControl],
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
          <SegmentedControl<Account>
            groupLabel="Account"
            current={account}
            onSelect={handleAccountChange}
            pending={pending}
            options={[
              { value: 'demo', label: 'Demo', color: 'var(--text-primary)' },
              {
                value: 'live',
                label: 'Live',
                color: 'var(--positive)',
                locked: !liveEnabled,
                lockLabel: 'Live account — requires HERMX_LIVE_TRADING=true',
              },
            ]}
          />
          <SegmentedControl<RiskState>
            groupLabel="Risk state"
            current={riskState}
            onSelect={handleRiskChange}
            pending={pending}
            options={[
              { value: 'active', label: 'Active', color: 'var(--text-primary)' },
              { value: 'reduce', label: 'Reduce', color: 'var(--warning, #e2b93d)' },
            ]}
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

      {/* Live-override provenance: when the operator armed this strategy. */}
      {account === 'live' && override?.set_at && (
        <p style={{ fontSize: 11, color: 'var(--text-muted)', margin: 0, textAlign: 'right' }}>
          live since {age(override.set_at)}
        </p>
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
        <Metric label="Performance" value={pct(perfPct)} color={perfColor} />
      </div>

      {/* Equity curve: cumulative realized net over closed episodes (closed-only;
          UPnL stays a separate scalar above). */}
      <div>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
          <span className="metric-label">PnL curve (closed)</span>
          {typeof strategy.accounting_start_at === 'number' && (
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
              P&L since {new Date(strategy.accounting_start_at).toISOString().slice(0, 10)}
            </span>
          )}
        </div>
        <Sparkline points={pnl?.pnl_series ?? []} />
      </div>
    </section>
  )
}

function Sparkline({ points }: { points: PnlPoint[] }) {
  const values = points.map((p) => p.cum_net ?? 0)
  if (values.length < 2) {
    return (
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
        Not enough closed trades
      </div>
    )
  }
  const w = 260
  const h = 36
  const pad = 2
  const min = Math.min(...values, 0)
  const max = Math.max(...values, 0)
  const range = max - min || 1
  const x = (i: number) => pad + (i * (w - 2 * pad)) / (values.length - 1)
  const y = (v: number) => h - pad - ((v - min) * (h - 2 * pad)) / range
  const path = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const last = values[values.length - 1]
  const stroke = last > 0 ? 'var(--positive)' : last < 0 ? 'var(--negative)' : 'var(--text-muted)'
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      width="100%"
      height={h}
      role="img"
      aria-label={`Cumulative realized PnL over ${values.length} closes, now ${last.toFixed(2)}`}
      style={{ display: 'block', marginTop: 4 }}
    >
      {min < 0 && max > 0 && (
        <line x1={pad} x2={w - pad} y1={y(0)} y2={y(0)} stroke="var(--border-dim)" strokeDasharray="3 3" />
      )}
      <polyline points={path} fill="none" stroke={stroke} strokeWidth={1.5} />
    </svg>
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
