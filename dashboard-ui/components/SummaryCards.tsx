'use client'
import type { CSSProperties } from 'react'
import { StatCard } from './StatCard'
import { useDashboardContext } from './DashboardProvider'
import { envBreakdown, money } from '../lib/format'
import { deriveSystemStatus } from '../lib/systemStatus'

// Mirrors src/dashboard.py:summary_cards() — four at-a-glance status cards.
type Kind = 'good' | 'bad' | 'warn' | 'muted'

function kindColor(kind: Kind): string {
  switch (kind) {
    case 'good':
      return 'var(--positive)'
    case 'bad':
      return 'var(--negative)'
    case 'warn':
      return 'var(--warning)'
    default:
      return 'var(--text-muted)'
  }
}

const gridStyle: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(4, 1fr)',
  gap: 12,
  marginBottom: 16,
}

export function SummaryCards() {
  const { data, health } = useDashboardContext()

  const strategies = data?.strategies ?? []
  const unattributed = data?.portfolio?.unattributed
  const positions = data?.okx_live?.positions ?? {}
  const executor = data?.executor

  // Card 1 — system status (shared ARMED predicate with ArmingBanner).
  const status = deriveSystemStatus(health?.arm)
  let sysLabel: string
  let sysKind: Kind
  if (status.kind === 'armed') {
    sysLabel = 'ARMED'
    sysKind = 'good'
  } else if (status.kind === 'demo') {
    sysLabel = 'DEMO'
    sysKind = 'warn'
  } else {
    sysLabel = 'DISARMED'
    sysKind = 'bad'
  }
  const sysSub = `${strategies.length} strategies active${
    status.killSwitchEngaged ? ' · kill switch ON' : ''
  }`

  // Card 2 — strategies.
  const demoCount = strategies.filter(
    (s) => (s.execution_mode ?? 'demo') !== 'live',
  ).length
  const liveCount = strategies.filter(
    (s) => (s.execution_mode ?? 'demo') === 'live',
  ).length
  const stratKind: Kind = strategies.length > 0 ? 'good' : 'muted'

  // Card 3 — open positions.
  const open = Object.values(positions).filter(
    (p) => (p.side ?? 'FLAT') !== 'FLAT',
  )
  const longs = open.filter((p) => p.side === 'LONG').length
  const shorts = open.filter((p) => p.side === 'SHORT').length
  const posKind: Kind = open.length > 0 ? 'good' : 'muted'

  // Card 4 — executor health.
  let execLabel: string
  let execKind: Kind
  if (!executor) {
    // No data yet (null/cold load) — distinct from an engine that errored.
    execLabel = '—'
    execKind = 'muted'
  } else if (executor.stale) {
    execLabel = 'STALE'
    execKind = 'warn'
  } else if (!executor.ok) {
    execLabel = 'ERROR'
    execKind = 'bad'
  } else {
    execLabel = 'OK'
    execKind = 'good'
  }

  const hermes = data?.hermes ?? {}
  const hermesOk = hermes.ok ?? false
  const hermesEnabled = hermes.enabled ?? false
  let hermesLabel: string
  let hermesColor: string
  if (!hermesEnabled) {
    hermesLabel = 'Hermes - Off'
    hermesColor = 'var(--text-muted)'
  } else if (hermesOk) {
    hermesLabel = 'Hermes - Ok'
    hermesColor = 'var(--positive)'
  } else {
    hermesLabel = 'Hermes - Error'
    hermesColor = 'var(--negative)'
  }

  const unattrCount = unattributed?.count ?? 0

  return (
    <>
      {unattrCount > 0 && (
        <div
          className="font-mono text-xs rounded px-3 py-2 border"
          style={{ borderColor: 'var(--warning)', color: 'var(--warning)', marginBottom: 12 }}
        >
          {unattrCount} close{unattrCount === 1 ? '' : 's'} unattributed ·{' '}
          {money(unattributed?.net_realized_pnl ?? 0)} net (pre-attribution history)
        </div>
      )}
      <div className="summary-cards" style={gridStyle}>
        <StatCard
          label="SYSTEM STATUS"
          value={sysLabel}
          sub={sysSub}
          accentColor={kindColor(sysKind)}
          valueColor={kindColor(sysKind)}
        />
        <StatCard
          label="STRATEGIES"
          value={String(strategies.length)}
          sub={`${demoCount} demo / ${liveCount} live`}
          accentColor={kindColor(stratKind)}
          valueColor={kindColor(stratKind)}
        />
        <StatCard
          label="OPEN POSITIONS"
          value={String(open.length)}
          sub={`${longs}L / ${shorts}S`}
          accentColor={kindColor(posKind)}
          valueColor={kindColor(posKind)}
        />
        <StatCard
          label="EXECUTION ENGINE"
          value={`Engine - ${execLabel}`}
          valueColor={kindColor(execKind)}
          value2={hermesLabel}
          value2Color={hermesColor}
          sub={envBreakdown(executor?.envs) ?? undefined}
          accentColor={kindColor(execKind)}
        />
      </div>
    </>
  )
}

export default SummaryCards
