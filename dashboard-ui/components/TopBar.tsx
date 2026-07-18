'use client'
import Link from 'next/link'
import { useDashboardContext } from './DashboardProvider'
import { FreshnessBadge } from './FreshnessBadge'
import { Badge } from './Badge'
import { age } from '../lib/format'

function ReconcileLagBadge() {
  const { data } = useDashboardContext()
  const lagMs = data?.reconcile_health?.reconcile_lag_ms
  if (lagMs === null || lagMs === undefined) return null
  const seconds = Math.round(lagMs / 1000)
  const label =
    seconds < 60
      ? `RECON ${seconds}s`
      : seconds < 3600
        ? `RECON ${Math.round(seconds / 60)}m`
        : `RECON ${Math.round(seconds / 3600)}h`
  return <Badge label={label} kind={seconds > 600 ? 'warn' : 'muted'} />
}

export function TopBar() {
  const { data, loading, error, lastUpdated } = useDashboardContext()

  const dotColor = error ? 'var(--warning)' : 'var(--positive)'
  // Trust header: the page keeps rendering last-good data on a failed poll, so
  // staleness (backend-declared or fetch error with data on screen) must be
  // explicit — the numbers below are as of data_at, not now.
  const stale = !!data?.freshness?.stale || (!!error && !!data)
  const dataAt = data?.freshness?.data_at ?? data?.generated_at ?? null

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
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
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
        <FreshnessBadge />
        <ReconcileLagBadge />
        <Link
          href="/health"
          style={{
            marginLeft: 'auto',
            fontFamily: 'var(--font-mono), monospace',
            fontSize: 11,
            color: 'var(--border-focus)',
          }}
        >
          Health →
        </Link>
      </div>
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
      </div>
      {stale && (
        <div
          role="status"
          aria-live="polite"
          className="font-mono text-xs rounded px-3 py-2 border"
          style={{
            marginTop: 8,
            borderColor: 'var(--warning)',
            color: 'var(--warning)',
            background: 'color-mix(in srgb, var(--warning) 8%, transparent)',
          }}
        >
          DATA STALE — as of {dataAt ? `${dataAt} (${age(dataAt)})` : '—'}
        </div>
      )}
    </header>
  )
}

export default TopBar
