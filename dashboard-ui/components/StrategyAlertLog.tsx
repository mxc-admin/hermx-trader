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

const trunc = (v: string | undefined, len: number) =>
  v && v.length > len ? `${v.slice(0, len)}…` : (v ?? '—')

const mono = { fontFamily: 'var(--font-mono), monospace' as const }

export function StrategyAlertLog() {
  const { data, strategyFilter } = useDashboardContext()
  const rows = ((data?.strategy_alerts ?? []) as Row[]).filter(
    (r) => !strategyFilter || r['strategy_id'] === strategyFilter
  )

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
      render: (row: Row) => {
        const v = pick(row, 'latency')
        return v === undefined ? '—' : `${v} ms`
      },
    },
  ]

  return (
    <Section title="STRATEGY ALERTS" defaultOpen={true}>
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
