import type { CSSProperties } from 'react'
import type { Strategy, LivePosition } from '../lib/types'
import { money, num, sideColor, sideKind } from '../lib/format'
import { Badge } from './Badge'

interface StrategyCardProps {
  strategy: Strategy
  position?: LivePosition
  alertCount: number
}

// Mirrors src/dashboard.py:strategy_card(). One wide card per active strategy:
// header (asset + name + live state), config strip, and the budget/PnL metrics.
export function StrategyCard({ strategy, position, alertCount }: StrategyCardProps) {
  const sym = strategy.asset ?? ''
  const side = (position?.side ?? 'FLAT').toUpperCase()
  const isLive = side !== 'FLAT'

  const submitEnabled = !!(strategy.submit_orders ?? strategy.okx_submit_orders)

  const budget = strategy.capital?.budget_usd ?? strategy.budget_usd ?? 0
  const realized = position?.realized_pnl ?? 0
  const upl = position?.upl ?? 0
  const equityNow = budget + realized + upl

  // Coincap icon pattern (base ticker, USDT suffix stripped).
  const logo = `https://assets.coincap.io/assets/icons/${sym
    .replace(/USDT?$/i, '')
    .toLowerCase()}@2x.png`

  const uplColor =
    upl > 0 ? 'var(--positive)' : upl < 0 ? 'var(--negative)' : 'var(--text-muted)'

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
          <Badge label={strategy.execution_mode ?? 'demo'} kind="neutral" />
          <Badge
            label={submitEnabled ? 'submit enabled' : 'orders disabled'}
            kind={submitEnabled ? 'good' : 'warn'}
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
