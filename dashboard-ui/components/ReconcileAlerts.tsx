'use client'
import { useDashboardContext } from './DashboardProvider'
import { DataTable } from './DataTable'
import { Badge } from './Badge'
import { age } from '../lib/format'
import { Section } from './Section'

// Mirrors dashboard.py reconcile_alert_table(). Rows are raw alerts.jsonl
// records (kind="reconcile"); `detail` is an open-ended object.
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

const asObj = (v: unknown): Row => (v && typeof v === 'object' ? (v as Row) : {})

const mono = { fontFamily: 'var(--font-mono), monospace' as const }

export function ReconcileAlerts() {
  const { data } = useDashboardContext()
  const rows = (data?.reconcile_alerts?.rows ?? []) as Row[]
  const stats = data?.reconcile_alerts?.stats

  const columns = [
    {
      key: 'time',
      header: 'Time',
      render: (row: Row) => age(str(pick(row, 'ts', 'received_at'))),
    },
    {
      key: 'kind',
      header: 'Kind',
      render: (row: Row) => {
        const detail = asObj(row['detail'])
        return str(pick(row, 'kind')) ?? str(pick(detail, 'stage')) ?? '—'
      },
    },
    {
      key: 'alert',
      header: 'Alert message',
      render: (row: Row) => {
        const v = str(pick(row, 'alert'))
        return v ? <Badge label={v} kind="warn" /> : '—'
      },
    },
    {
      key: 'detail',
      header: 'Detail',
      render: (row: Row) => {
        const detail = asObj(row['detail'])
        const cl = str(pick(detail, 'cl_ord_id'))
        const sym = str(pick(detail, 'symbol'))
        const parts = [cl, sym].filter(Boolean)
        return (
          <span style={{ ...mono, color: 'var(--text-muted)' }}>
            {parts.length ? parts.join(' · ') : '—'}
          </span>
        )
      },
    },
  ]

  return (
    <Section title="RECONCILE ALERTS" defaultOpen={false}>
      <DataTable<Row>
        columns={columns}
        rows={rows}
        emptyMessage="No reconcile alerts"
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

export default ReconcileAlerts
