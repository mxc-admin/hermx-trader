'use client'
import { useDashboardContext } from './DashboardProvider'
import { DataTable } from './DataTable'
import { Badge } from './Badge'
import { age } from '../lib/format'
import { Section } from './Section'

// Mirrors dashboard.py operator_alert_table(). Rows are raw alerts.jsonl
// records (kind="operator"); `detail` is an open-ended object.
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

// Matches operator_alert_table severity mapping in dashboard.py.
const severityKindOf = (v: string) => {
  const u = v.toLowerCase()
  if (u === 'error') return 'bad' as const
  if (u === 'warn' || u === 'warning') return 'warn' as const
  return 'neutral' as const
}

const detailStr = (v: unknown): string => {
  if (v && typeof v === 'object') {
    const entries = Object.entries(v as Row).slice(0, 8)
    if (!entries.length) return '—'
    return entries.map(([k, val]) => `${k}=${val}`).join(', ')
  }
  return str(v) ?? '—'
}

const mono = { fontFamily: 'var(--font-mono), monospace' as const }

export function OperatorAlerts() {
  const { data } = useDashboardContext()
  const rows = (data?.operator_alerts?.rows ?? []) as Row[]
  const stats = data?.operator_alerts?.stats

  const columns = [
    {
      key: 'time',
      header: 'Time',
      render: (row: Row) => age(str(pick(row, 'ts', 'received_at'))),
    },
    {
      key: 'severity',
      header: 'Severity',
      render: (row: Row) => {
        const v = str(pick(row, 'severity')) ?? 'warning'
        return <Badge label={v} kind={severityKindOf(v)} />
      },
    },
    {
      key: 'alert',
      header: 'Alert message',
      render: (row: Row) => str(pick(row, 'alert')) ?? '—',
    },
    {
      key: 'detail',
      header: 'Detail',
      render: (row: Row) => (
        <span style={{ ...mono, color: 'var(--text-muted)' }}>
          {detailStr(row['detail'])}
        </span>
      ),
    },
  ]

  return (
    <Section title="OPERATOR ALERTS" defaultOpen={false}>
      <DataTable<Row>
        columns={columns}
        rows={rows}
        emptyMessage="No operator alerts"
      />
      <div
        style={{
          ...mono,
          padding: '6px 12px',
          fontSize: 10,
          color: 'var(--text-muted)',
        }}
      >
        {`${stats?.read ?? rows.length} read | ${stats?.skipped ?? 0} skipped`}
      </div>
    </Section>
  )
}

export default OperatorAlerts
