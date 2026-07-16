'use client'
import { useDashboardContext } from './DashboardProvider'
import { age } from '../lib/format'

export function TopBar() {
  const { data, loading, error, lastUpdated, strategyFilter, setStrategyFilter } =
    useDashboardContext()

  const dotColor = error ? 'var(--warning)' : 'var(--positive)'
  const strategyIds = (data?.strategies ?? [])
    .map((s) => s.strategy_id)
    .filter((sid): sid is string => Boolean(sid))

  return (
    <header style={{ padding: '16px 0' }}>
      <div
        style={{
          fontFamily: 'var(--font-mono), monospace',
          fontSize: 10,
          letterSpacing: '0.12em',
          textTransform: 'uppercase',
          color: 'var(--text-muted)',
        }}
      >
        Kinetic Flow by Momentumx
      </div>
      <h1
        style={{
          margin: '2px 0 6px',
          fontSize: 24,
          fontWeight: 700,
          color: 'var(--text-primary)',
        }}
      >
        HermX Dashboard
      </h1>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span
          aria-hidden
          className={!loading ? 'live-dot' : undefined}
          style={{
            width: 5, height: 5, borderRadius: '50%', display: 'inline-block',
            background: dotColor, boxShadow: `0 0 5px ${dotColor}`,
          }}
        />
        <span style={{ fontFamily: 'var(--font-mono), monospace', fontSize: 10, color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
          {error ? <span style={{ color: 'var(--negative)' }}>{error}</span> : `Updated ${age(lastUpdated ? lastUpdated.toISOString() : null)}`}
        </span>
        <select
          aria-label="Filter by strategy"
          value={strategyFilter ?? ''}
          onChange={(e) => setStrategyFilter(e.target.value || null)}
          style={{
            marginLeft: 'auto',
            fontFamily: 'var(--font-mono), monospace',
            fontSize: 11,
            padding: '2px 6px',
            background: 'var(--bg-panel)',
            color: 'var(--text-secondary)',
            border: '1px solid var(--border-dim)',
            borderRadius: 4,
          }}
        >
          <option value="">All strategies</option>
          {strategyIds.map((sid) => (
            <option key={sid} value={sid}>{sid}</option>
          ))}
        </select>
      </div>
    </header>
  )
}

export default TopBar
