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

const asset = (row: PositionRow) => row.symbol ?? row.inst_id ?? '—'

export function PositionsTable() {
  const { data, strategyFilter } = useDashboardContext()
  const positions = data?.positions
  const byStrategy = (rows: PositionRow[] | undefined) =>
    (rows ?? []).filter((r) => !strategyFilter || r.strategy_id === strategyFilter)
  const open = byStrategy(positions?.open)
  const closed = byStrategy(positions?.closed)
  const driftCount = positions?.drift?.count ?? 0

  const openColumns = [
    { key: 'strategy', header: 'Strategy', render: (r: PositionRow) => r.strategy_id ?? '—' },
    { key: 'asset', header: 'Asset', render: asset },
    { key: 'env', header: 'Env', render: (r: PositionRow) => `${r.venue ?? '—'}:${r.mode ?? '—'}` },
    { key: 'side', header: 'Side', render: (r: PositionRow) => <SideBadge side={r.side} /> },
    { key: 'qty', header: 'Qty', render: (r: PositionRow) => num(r.qty, 4) },
    { key: 'entry', header: 'Entry', render: (r: PositionRow) => num(r.entry_px, 4) },
    { key: 'mark', header: 'Mark', render: (r: PositionRow) => num(r.mark_px, 4) },
    { key: 'upl', header: 'UPnL', render: (r: PositionRow) => <Pnl value={r.upl} /> },
    { key: 'opened', header: 'Opened', render: (r: PositionRow) => ageMs(r.opened_at_ms) },
  ]

  const closedColumns = [
    { key: 'strategy', header: 'Strategy', render: (r: PositionRow) => r.strategy_id ?? '—' },
    { key: 'asset', header: 'Asset', render: asset },
    { key: 'env', header: 'Env', render: (r: PositionRow) => `${r.venue ?? '—'}:${r.mode ?? '—'}` },
    { key: 'side', header: 'Side', render: (r: PositionRow) => <SideBadge side={r.side} /> },
    { key: 'qty', header: 'Qty', render: (r: PositionRow) => num(r.qty, 4) },
    { key: 'entry', header: 'Entry', render: (r: PositionRow) => num(r.entry_px, 4) },
    { key: 'exit', header: 'Exit', render: (r: PositionRow) => num(r.exit_px, 4) },
    { key: 'net', header: 'Net PnL', render: (r: PositionRow) => <Pnl value={r.realized_pnl_net} /> },
    { key: 'fees', header: 'Fees', render: (r: PositionRow) => money(r.fees ?? null) },
    { key: 'opened', header: 'Opened', render: (r: PositionRow) => ageMs(r.opened_at_ms) },
    { key: 'closed', header: 'Closed', render: (r: PositionRow) => ageMs(r.closed_at_ms) },
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
      />
      <div style={{ ...mono, padding: '10px 12px 2px', fontSize: 10, letterSpacing: '0.08em', color: 'var(--text-muted)' }}>
        CLOSED
      </div>
      <DataTable<PositionRow>
        label="Closed positions"
        columns={closedColumns}
        rows={closed}
        emptyMessage="No closed positions"
      />
    </Section>
  )
}

export default PositionsTable
