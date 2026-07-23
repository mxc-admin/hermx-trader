'use client'
import { useDashboardContext } from './DashboardProvider'
import { DataTable } from './DataTable'
import { Badge } from './Badge'
import { age } from '../lib/format'
import { Section } from './Section'

// Mirrors dashboard.py strategy_alert_table(). Rows are raw strategy_alerts records.
type Row = Record<string, unknown>

const str = (v: unknown): string | undefined =>
  v === null || v === undefined || v === '' ? undefined : String(v)

const pick = (row: Row, ...keys: string[]): unknown => {
  for (const k of keys) {
    const v = row[k]
    if (v !== null && v !== undefined && v !== '') return v
  }
  return undefined
}

const signalKindOf = (v: string) => {
  const u = v.toUpperCase()
  if (u === 'BUY' || u === 'LONG') return 'good' as const
  if (u === 'SELL' || u === 'SHORT') return 'bad' as const
  return 'neutral' as const
}

const decisionKindOf = (v: string) => {
  const u = v.toUpperCase()
  if (u === 'TRADE') return 'good' as const
  if (u === 'SKIP' || u === 'DUPLICATE') return 'muted' as const
  if (u === 'BLOCKED') return 'bad' as const
  return 'neutral' as const
}

// Server-joined execution outcome (exact received_at match). A missing outcome
// (historical rows, in-flight signals) renders as a plain dash — never "orphan".
const outcomeKindOf = (v: string) => {
  const u = v.toUpperCase()
  if (u === 'FILLED') return 'good' as const
  if (u === 'BLOCKED') return 'bad' as const
  if (u === 'NO FILL') return 'warn' as const
  return 'neutral' as const
}

const trunc = (v: string | undefined, len: number) =>
  v && v.length > len ? `${v.slice(0, len)}…` : (v ?? '—')

const mono = { fontFamily: 'var(--font-mono), monospace' as const }

export function StrategyAlertLog() {
  const { data, strategyFilter, positionFilter, setPositionFilter } = useDashboardContext()
  // Position click filter: alerts carry no cl_ord_id, so join through the
  // execution rows -- cl_ord_id -> the exec row's received_at -> the alert's
  // received_at (the exact intake join key, same chain the server outcome join
  // uses). An empty chain matches nothing — zero alerts beats wrong alerts.
  let alertKeys: Set<string> | null = null
  if (positionFilter) {
    const ids = new Set(positionFilter.clOrdIds)
    alertKeys = new Set()
    for (const r of (data?.okx_executions ?? []) as Row[]) {
      const cl = str(pick(r, 'cl_ord_id', 'client_order_id'))
      const ra = str(r['received_at'])
      if (cl !== undefined && ra !== undefined && ids.has(cl)) alertKeys.add(ra)
    }
  }
  const rows = ((data?.strategy_alerts ?? []) as Row[])
    .filter((r) => !strategyFilter || r['strategy_id'] === strategyFilter)
    .filter((r) => {
      if (!alertKeys) return true
      const ra = str(r['received_at'])
      return ra !== undefined && alertKeys.has(ra)
    })
    .reverse() // server rows are oldest-first (legacy table reverses too); show newest-first

  const columns = [
    {
      key: 'tv_time',
      header: 'TV Time',
      render: (row: Row) =>
        age(str(pick(row, 'tv_time', 'tv_time_colombia', 'received_at', 'received_colombia'))),
    },
    {
      key: 'strategy',
      header: 'Strategy',
      render: (row: Row) => (
        <span style={{ ...mono, color: 'var(--text-secondary)', fontSize: '11px' }}>
          {str(pick(row, 'strategy_id')) ?? '—'}
        </span>
      ),
    },
    {
      key: 'asset',
      header: 'Asset',
      render: (row: Row) => str(pick(row, 'asset')) ?? '—',
    },
    {
      key: 'tf',
      header: 'TF',
      render: (row: Row) => str(pick(row, 'timeframe')) ?? '—',
    },
    {
      key: 'signal',
      header: 'Signal',
      render: (row: Row) => {
        const v = str(pick(row, 'side', 'signal'))
        return v ? <Badge label={v} kind={signalKindOf(v)} /> : '—'
      },
    },
    {
      key: 'decision',
      header: 'Decision',
      render: (row: Row) => {
        const v = row['duplicate'] ? 'DUPLICATE' : str(pick(row, 'decision'))
        return v ? <Badge label={v} kind={decisionKindOf(v)} /> : '—'
      },
    },
    {
      key: 'outcome',
      header: 'Outcome',
      render: (row: Row) => {
        const v = str(row['outcome'])
        return v ? <Badge label={v} kind={outcomeKindOf(v)} /> : '—'
      },
    },
    {
      key: 'block_reason',
      header: 'Block reason',
      render: (row: Row) => (
        <span style={{ ...mono, color: 'var(--text-muted)' }}>
          {trunc(str(pick(row, 'block_reason')), 40)}
        </span>
      ),
    },
    {
      key: 'latency',
      header: 'Latency',
      // Backend value is latency_seconds (model.py stamps `latency` from
      // latency.latency_seconds) — label it as seconds, like render.py fmt_seconds.
      render: (row: Row) => {
        const v = pick(row, 'latency')
        return v === undefined ? '—' : `${v} s`
      },
    },
  ]

  return (
    <Section title="STRATEGY ALERTS" defaultOpen={true}>
      {positionFilter && (
        <div
          role="status"
          style={{
            ...mono,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            padding: '6px 12px',
            fontSize: 11,
            color: 'var(--text-secondary)',
          }}
        >
          <span>Position filter: {positionFilter.label}</span>
          <button
            type="button"
            onClick={() => setPositionFilter(null)}
            style={{
              ...mono,
              fontSize: 11,
              padding: '1px 8px',
              borderRadius: 4,
              border: '1px solid var(--border-dim)',
              background: 'transparent',
              color: 'var(--text-secondary)',
              cursor: 'pointer',
            }}
          >
            Clear ✕
          </button>
        </div>
      )}
      <DataTable<Row>
        label="Strategy alerts"
        columns={columns}
        rows={rows}
        emptyMessage="No alerts received"
      />
    </Section>
  )
}

export default StrategyAlertLog
