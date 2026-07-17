'use client'
import { useDashboardContext } from './DashboardProvider'
import { DataTable } from './DataTable'
import { Badge } from './Badge'
import { money, num, age, sideKind } from '../lib/format'
import { Section } from './Section'
import { StrategyFilter } from './StrategyFilter'
import type { PositionRow } from '../lib/types'

const mono = { fontFamily: 'var(--font-mono), monospace' as const }

const ageMs = (ms: number | null | undefined) =>
  ms ? age(new Date(ms).toISOString()) : '—'

// Relative age with the full ISO timestamp on hover.
const AgeMs = ({ ms }: { ms: number | null | undefined }) =>
  ms ? <span title={new Date(ms).toISOString()}>{ageMs(ms)}</span> : <>—</>

const TYPE_SUFFIXES = new Set(['SWAP', 'FUTURES', 'FUTURE', 'PERP', 'SPOT', 'MARGIN', 'OPTION'])

// BTC-USDT-SWAP / BTC/USDT:USDT -> BTCUSDT (display-only compaction, mirrors
// the backend strategy_asset derivation).
const compactInstId = (instId: string) => {
  const parts = instId.split(':', 1)[0].split(/[-/]/).filter(Boolean)
  if (parts.length >= 3 && TYPE_SUFFIXES.has(parts[parts.length - 1].toUpperCase())) {
    parts.pop()
  }
  return parts.join('').toUpperCase()
}

const pnlColor = (n: number | null | undefined) =>
  n === null || n === undefined
    ? 'var(--text-muted)'
    : n > 0
      ? 'var(--positive)'
      : n < 0
        ? 'var(--negative)'
        : 'var(--text-secondary)'

const Pnl = ({ value }: { value: number | null | undefined }) => (
  <span style={{ ...mono, color: pnlColor(value) }}>{money(value ?? null)}</span>
)

const SideBadge = ({ side }: { side: string | null | undefined }) =>
  side ? <Badge label={side.toUpperCase()} kind={sideKind(side)} /> : <>—</>

const asset = (row: PositionRow) =>
  row.symbol ?? (row.inst_id ? compactInstId(row.inst_id) : '—')

// Stable click identity for a position row (episodes have no server id).
const rowKey = (r: PositionRow) =>
  [r.status, r.strategy_id, r.venue, r.mode, r.inst_id, r.opened_at_ms, r.closed_at_ms].join('|')

export function PositionsTable() {
  const { data, strategyFilter, positionFilter, setPositionFilter } = useDashboardContext()
  const positions = data?.positions
  const byStrategy = (rows: PositionRow[] | undefined) =>
    (rows ?? []).filter((r) => !strategyFilter || r.strategy_id === strategyFilter)
  const open = byStrategy(positions?.open)
  const closed = byStrategy(positions?.closed)
  const driftCount = positions?.drift?.count ?? 0

  // Click a position -> filter EXECUTION EVENTS to exactly its orders (exact
  // cl_ord_id match). Click the same row again to clear.
  const toggleSelect = (r: PositionRow) => {
    const key = rowKey(r)
    if (positionFilter?.key === key) {
      setPositionFilter(null)
      return
    }
    setPositionFilter({
      key,
      label: `${asset(r)} ${r.status ?? ''}${r.strategy_id ? ` · ${r.strategy_id}` : ''}`.trim(),
      clOrdIds: [...(r.open_cl_ord_ids ?? []), ...(r.close_cl_ord_ids ?? [])],
    })
  }
  const isSelected = (r: PositionRow) => positionFilter?.key === rowKey(r)

  const openColumns = [
    { key: 'opened', header: 'Opened', render: (r: PositionRow) => <AgeMs ms={r.opened_at_ms} /> },
    { key: 'strategy', header: 'Strategy', render: (r: PositionRow) => r.strategy_id ?? '—' },
    { key: 'asset', header: 'Asset', render: asset },
    { key: 'env', header: 'Env', render: (r: PositionRow) => `${r.venue ?? '—'}:${r.mode ?? '—'}` },
    { key: 'side', header: 'Side', render: (r: PositionRow) => <SideBadge side={r.side} /> },
    { key: 'qty', header: 'Qty', render: (r: PositionRow) => num(r.qty, 4) },
    { key: 'entry', header: 'Entry', render: (r: PositionRow) => num(r.entry_px, 4) },
    { key: 'mark', header: 'Mark', render: (r: PositionRow) => num(r.mark_px, 4) },
    { key: 'upl', header: 'UPnL', render: (r: PositionRow) => <Pnl value={r.upl} /> },
  ]

  const closedColumns = [
    { key: 'opened', header: 'Opened', render: (r: PositionRow) => <AgeMs ms={r.opened_at_ms} /> },
    { key: 'strategy', header: 'Strategy', render: (r: PositionRow) => r.strategy_id ?? '—' },
    { key: 'asset', header: 'Asset', render: asset },
    { key: 'env', header: 'Env', render: (r: PositionRow) => `${r.venue ?? '—'}:${r.mode ?? '—'}` },
    { key: 'side', header: 'Side', render: (r: PositionRow) => <SideBadge side={r.side} /> },
    { key: 'qty', header: 'Qty', render: (r: PositionRow) => num(r.qty, 4) },
    { key: 'entry', header: 'Entry', render: (r: PositionRow) => num(r.entry_px, 4) },
    { key: 'exit', header: 'Exit', render: (r: PositionRow) => num(r.exit_px, 4) },
    { key: 'net', header: 'Net PnL', render: (r: PositionRow) => <Pnl value={r.realized_pnl_net} /> },
    { key: 'fees', header: 'Fees', render: (r: PositionRow) => money(r.fees ?? null) },
    { key: 'closed', header: 'Closed', render: (r: PositionRow) => <AgeMs ms={r.closed_at_ms} /> },
  ]

  return (
    <Section title="POSITIONS" defaultOpen={true} actions={<StrategyFilter />}>
      {driftCount > 0 && (
        <div
          role="status"
          style={{
            ...mono,
            padding: '6px 12px',
            fontSize: 11,
            color: 'var(--warning)',
          }}
        >
          {driftCount} position drift{driftCount === 1 ? '' : 's'} detected
          (ledger vs venue — observe-only, no action taken)
        </div>
      )}
      <div style={{ ...mono, padding: '6px 12px 2px', fontSize: 10, letterSpacing: '0.08em', color: 'var(--text-muted)' }}>
        OPEN
      </div>
      <DataTable<PositionRow>
        label="Open positions"
        columns={openColumns}
        rows={open}
        emptyMessage="No open positions"
        maxHeight="240px"
        onRowClick={toggleSelect}
        rowSelected={isSelected}
      />
      <div style={{ ...mono, padding: '10px 12px 2px', fontSize: 10, letterSpacing: '0.08em', color: 'var(--text-muted)' }}>
        CLOSED
      </div>
      <DataTable<PositionRow>
        label="Closed positions"
        columns={closedColumns}
        rows={closed}
        emptyMessage="No closed positions"
        onRowClick={toggleSelect}
        rowSelected={isSelected}
      />
      <div style={{ ...mono, padding: '4px 12px', fontSize: 10, color: 'var(--text-muted)' }}>
        Click a position to filter execution events to its orders
      </div>
    </Section>
  )
}

export default PositionsTable
