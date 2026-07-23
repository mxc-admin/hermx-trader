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

// Mirrors render.py exchange_leg_label/exchange_leg_kind: okx_action is the
// executed leg (OPEN_LONG / CLOSE_SHORT / ...), truer than the alert side.
const ACTION_LABELS: Record<string, string> = {
  OPEN_LONG: 'Open Long',
  OPEN_SHORT: 'Open Short',
  CLOSE_LONG: 'Close Long',
  CLOSE_SHORT: 'Close Short',
}

const actionKindOf = (v: string) => {
  const u = v.toUpperCase()
  if (u.startsWith('OPEN_LONG') || u.startsWith('CLOSE_SHORT')) return 'good' as const
  if (u.startsWith('OPEN_SHORT') || u.startsWith('CLOSE_LONG')) return 'bad' as const
  return 'neutral' as const
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
  const { data, strategyFilter, positionFilter, setPositionFilter } = useDashboardContext()
  const selected = (data?.strategies ?? []).find(
    (s) => s.strategy_id === strategyFilter
  )
  // Position click filter: EXACT cl_ord_id membership. An empty id list (legacy
  // episodes without recorded cl_ord_ids) matches nothing — zero events beats
  // wrong events.
  const positionIds = positionFilter ? new Set(positionFilter.clOrdIds) : null
  const rows = ((data?.okx_executions ?? []) as Row[])
    .filter(
      r => r['status'] !== 'not_submitted' && r['order_status'] !== 'not_submitted'
    )
    // New rows carry a stamped strategy_id (exact match); historical rows fall
    // back to the selected strategy's asset symbol.
    .filter(
      r =>
        !strategyFilter ||
        r['strategy_id'] === strategyFilter ||
        (selected?.asset !== undefined && r['symbol'] === selected.asset)
    )
    .filter(r => {
      if (!positionIds) return true
      const cl = str(pick(r, 'cl_ord_id', 'client_order_id'))
      return cl !== undefined && positionIds.has(cl)
    })
    .reverse() // pipeline rows arrive oldest-first; show newest-first
  const skipped = data?.ledger_health?.total_skipped

  const columns = [
    {
      key: 'time',
      // Exec rows carry tv_time=None (backend hardcodes it), so this is
      // effectively the intake received time — label it as such, with the raw
      // ISO on hover (PositionsTable AgeMs pattern).
      header: 'Received',
      render: (row: Row) => {
        const iso = str(pick(row, 'tv_time', 'submitted_at', 'received_at', 'received_colombia'))
        return iso ? <span title={iso}>{age(iso)}</span> : '—'
      },
    },
    {
      key: 'asset',
      header: 'Asset',
      render: (row: Row) => str(pick(row, 'symbol', 'strategy_id', 'inst_id')) ?? '—',
    },
    {
      key: 'action',
      header: 'Action',
      render: (row: Row) => {
        const action = str(row['okx_action'])
        if (action && action !== '-') {
          const label = ACTION_LABELS[action.toUpperCase()] ?? action
          return <Badge label={label} kind={actionKindOf(action)} />
        }
        // Rows without an executed leg (older/sparse records) fall back to the
        // alert side so the column never goes blank retroactively.
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
    // Per-event PnL intentionally hidden: position-level Net PnL (PositionsTable)
    // is the accounting truth; the enrichment path still populates realized_pnl
    // on rows for the details payload and future use.
  ]

  return (
    <Section title="EXECUTION EVENTS" defaultOpen={true}>
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
          <span>
            Position filter: {positionFilter.label} ({positionFilter.clOrdIds.length} order
            {positionFilter.clOrdIds.length === 1 ? '' : 's'})
          </span>
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
