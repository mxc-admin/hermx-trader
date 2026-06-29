'use client'
import { useDashboardContext } from './DashboardProvider'
import { DataTable } from './DataTable'
import { Badge } from './Badge'
import { num, age } from '../lib/format'
import { Section } from './Section'

// Mirrors dashboard.py open_orders_table(). Rows are order-journal records in
// non-terminal states (open_orders.rows); stats is the bounded-reader summary.
type Row = Record<string, unknown>

const str = (v: unknown): string | undefined =>
  v === null || v === undefined || v === '' ? undefined : String(v)

const numeric = (v: unknown): number | string | null =>
  typeof v === 'number' || typeof v === 'string' ? v : null

const pick = (row: Row, ...keys: string[]): unknown => {
  for (const k of keys) {
    const v = row[k]
    if (v !== null && v !== undefined && v !== '') return v
  }
  return undefined
}

// Matches _state_kind() in dashboard.py.
const stateKindOf = (v: string) => {
  const u = v.toUpperCase()
  if (u === 'SUBMITTED') return 'good' as const
  if (u === 'UNKNOWN') return 'warn' as const
  return 'neutral' as const
}

const sideKindOf = (v: string) => {
  const u = v.toUpperCase()
  if (u === 'LONG' || u === 'BUY') return 'good' as const
  if (u === 'SHORT' || u === 'SELL') return 'bad' as const
  return 'muted' as const
}

const trunc = (v: string | undefined, len: number) =>
  v && v.length > len ? `${v.slice(0, len)}…` : (v ?? '—')

const mono = { fontFamily: 'var(--font-mono), monospace' as const }

export function OpenOrdersTable() {
  const { data } = useDashboardContext()
  const rows = (data?.open_orders?.rows ?? []) as Row[]
  const stats = data?.open_orders?.stats

  const columns = [
    {
      key: 'time',
      header: 'Time',
      render: (row: Row) => age(str(pick(row, 'ts', 'received_at'))),
    },
    {
      key: 'asset',
      header: 'Asset',
      render: (row: Row) => str(pick(row, 'symbol', 'inst_id')) ?? '—',
    },
    {
      key: 'side',
      header: 'Side',
      render: (row: Row) => {
        const v = str(pick(row, 'side'))
        return v ? <Badge label={v} kind={sideKindOf(v)} /> : '—'
      },
    },
    {
      key: 'state',
      header: 'State',
      render: (row: Row) => {
        const v = str(pick(row, 'state'))
        return v ? <Badge label={v} kind={stateKindOf(v)} /> : '—'
      },
    },
    {
      key: 'fill_px',
      header: 'Fill Px',
      render: (row: Row) => num(numeric(pick(row, 'fill_px', 'px', 'avg_px')), 4),
    },
    {
      key: 'qty',
      header: 'Qty',
      render: (row: Row) => num(numeric(pick(row, 'qty', 'sz', 'contracts')), 4),
    },
    {
      key: 'cl_ord_id',
      header: 'cl_ord_id',
      render: (row: Row) => (
        <span style={mono}>{trunc(str(pick(row, 'cl_ord_id')), 16)}</span>
      ),
    },
  ]

  return (
    <Section title="OPEN ORDERS" defaultOpen={false}>
      <DataTable<Row>
        columns={columns}
        rows={rows}
        emptyMessage="No open orders"
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

export default OpenOrdersTable
