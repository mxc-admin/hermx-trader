'use client'
import { useDashboardContext } from './DashboardProvider'
import { DataTable } from './DataTable'
import { Badge } from './Badge'
import { money, num, age } from '../lib/format'
import { Section } from './Section'

// Mirrors dashboard.py okx_execution_table(). Rows are raw okx_executions
// records; field names vary, so every access is defensive.
type Row = Record<string, unknown>

const str = (v: unknown): string | undefined =>
  v === null || v === undefined || v === '' ? undefined : String(v)

const numeric = (v: unknown): number | string | null =>
  typeof v === 'number' || typeof v === 'string' ? v : null

// money() only accepts number|null, so coerce strings here.
const moneyNum = (v: unknown): number | null => {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string' && v !== '') {
    const n = Number(v)
    return Number.isFinite(n) ? n : null
  }
  return null
}

const pick = (row: Row, ...keys: string[]): unknown => {
  for (const k of keys) {
    const v = row[k]
    if (v !== null && v !== undefined && v !== '') return v
  }
  return undefined
}

const sideKindOf = (v: string) => {
  const u = v.toUpperCase()
  if (u === 'LONG' || u === 'BUY') return 'good' as const
  if (u === 'SHORT' || u === 'SELL') return 'bad' as const
  return 'muted' as const
}

const stateKindOf = (v: string) => {
  const u = v.toUpperCase()
  if (u === 'FILLED') return 'good' as const
  if (u === 'REJECTED') return 'bad' as const
  if (u === 'UNKNOWN') return 'warn' as const
  return 'neutral' as const
}

const mono = { fontFamily: 'var(--font-mono), monospace' as const }

export function ExecutionLedger() {
  const { data, strategyFilter } = useDashboardContext()
  const selected = (data?.strategies ?? []).find(
    (s) => s.strategy_id === strategyFilter
  )
  const rows = ((data?.okx_executions ?? []) as Row[])
    .filter(
      r => r['status'] !== 'not_submitted' && r['order_status'] !== 'not_submitted'
    )
    // Rows carry no strategy_id, so the strategy filter matches on the selected
    // strategy's asset symbol (falling back to strategy_id when a row has one).
    .filter(
      r =>
        !strategyFilter ||
        r['strategy_id'] === strategyFilter ||
        (selected?.asset !== undefined && r['symbol'] === selected.asset)
    )
    .reverse() // pipeline rows arrive oldest-first; show newest-first
  const skipped = data?.ledger_health?.total_skipped

  const columns = [
    {
      key: 'time',
      header: 'Time',
      render: (row: Row) =>
        age(str(pick(row, 'tv_time', 'submitted_at', 'received_at', 'received_colombia'))),
    },
    {
      key: 'asset',
      header: 'Asset',
      render: (row: Row) => str(pick(row, 'symbol', 'strategy_id', 'inst_id')) ?? '—',
    },
    {
      key: 'side',
      header: 'Side',
      render: (row: Row) => {
        const v = str(pick(row, 'signal', 'okx_side', 'side'))
        return v ? <Badge label={v} kind={sideKindOf(v)} /> : '—'
      },
    },
    {
      key: 'fill_px',
      header: 'Fill Px',
      render: (row: Row) => num(numeric(pick(row, 'okx_price', 'alert_price')), 4),
    },
    {
      key: 'notional',
      header: 'Notional',
      render: (row: Row) => money(moneyNum(pick(row, 'notional', 'planned_notional'))),
    },
    {
      key: 'state',
      header: 'State',
      render: (row: Row) => {
        const v = str(pick(row, 'order_status', 'status'))
        return v ? <Badge label={v} kind={stateKindOf(v)} /> : '—'
      },
    },
    {
      key: 'pnl',
      header: 'PnL',
      render: (row: Row) => {
        const n = moneyNum(pick(row, 'realized_pnl'))
        const color =
          n === null
            ? 'var(--text-muted)'
            : n > 0
              ? 'var(--positive)'
              : n < 0
                ? 'var(--negative)'
                : 'var(--text-secondary)'
        return <span style={{ ...mono, color }}>{money(n)}</span>
      },
    },
  ]

  return (
    <Section title="EXECUTION EVENTS" defaultOpen={true}>
      <DataTable<Row>
        label="Execution events"
        columns={columns}
        rows={rows}
        emptyMessage="No executions recorded"
      />
      <div
        style={{
          ...mono,
          padding: '6px 12px',
          fontSize: 10,
          color: 'var(--text-muted)',
        }}
      >
        {rows.length} rows
        {typeof skipped === 'number' ? ` | ${skipped} skipped` : ''}
      </div>
    </Section>
  )
}

export default ExecutionLedger
