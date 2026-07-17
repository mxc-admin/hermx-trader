import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// Mock the context hook: StrategyAlertLog only reads data + filters from it.
vi.mock('./DashboardProvider', () => ({
  useDashboardContext: vi.fn(),
}))

import { useDashboardContext } from './DashboardProvider'
import { StrategyAlertLog } from './StrategyAlertLog'

const mockCtx = vi.mocked(useDashboardContext)

const OLD_RA = '2026-07-01T00:00:00.000001+00:00'
const NEW_RA = '2026-07-02T00:00:00.000002+00:00'

// Server contract: strategy_alerts arrive sorted OLDEST-first.
const alerts = [
  {
    received_at: OLD_RA,
    tv_time: '2026-07-01T00:00:00Z',
    strategy_id: 'strat-a',
    asset: 'OLDASSET',
    side: 'BUY',
    outcome: 'FILLED',
    block_reason: null,
  },
  {
    received_at: NEW_RA,
    tv_time: '2026-07-02T00:00:00Z',
    strategy_id: 'strat-b',
    asset: 'NEWASSET',
    side: 'SELL',
    outcome: 'BLOCKED',
    block_reason: 'live_trading_disabled',
  },
]

const executions = [
  { received_at: OLD_RA, cl_ord_id: 'cid-old' },
  { received_at: NEW_RA, client_order_id: 'cid-new' },
]

function setCtx(overrides: Record<string, unknown> = {}) {
  mockCtx.mockReturnValue({
    data: { strategy_alerts: alerts, okx_executions: executions },
    strategyFilter: null,
    setStrategyFilter: vi.fn(),
    positionFilter: null,
    setPositionFilter: vi.fn(),
    ...overrides,
  } as never)
}

const bodyRows = () => {
  const table = screen.getByRole('table', { name: 'Strategy alerts' })
  return Array.from(table.querySelectorAll('tbody tr'))
}

beforeEach(() => {
  mockCtx.mockReset()
})

describe('StrategyAlertLog ordering', () => {
  it('renders newest alert first (server rows are oldest-first)', () => {
    setCtx()
    render(<StrategyAlertLog />)
    const rows = bodyRows()
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('NEWASSET')
    expect(rows[1].textContent).toContain('OLDASSET')
  })
})

describe('StrategyAlertLog position filter', () => {
  const selection = { key: 'k', label: 'BTCUSDT open · strat-a', clOrdIds: ['cid-old'] }

  it('filters alerts to the selected position via cl_ord_id -> received_at join', () => {
    setCtx({ positionFilter: selection })
    render(<StrategyAlertLog />)
    const rows = bodyRows()
    expect(rows).toHaveLength(1)
    expect(rows[0].textContent).toContain('OLDASSET')
  })

  it('matches nothing when the selection has no recorded cl_ord_ids', () => {
    setCtx({ positionFilter: { ...selection, clOrdIds: [] } })
    render(<StrategyAlertLog />)
    expect(screen.getByText('No alerts received')).toBeInTheDocument()
  })

  it('shows the filter chip and clears via its button', () => {
    const setPositionFilter = vi.fn()
    setCtx({ positionFilter: selection, setPositionFilter })
    render(<StrategyAlertLog />)
    expect(screen.getByRole('status').textContent).toContain('BTCUSDT open · strat-a')

    fireEvent.click(screen.getByRole('button', { name: 'Clear ✕' }))
    expect(setPositionFilter).toHaveBeenCalledWith(null)
  })
})

describe('StrategyAlertLog outcome / block reason columns', () => {
  it('renders outcome badges and the block reason from backend fields', () => {
    setCtx()
    render(<StrategyAlertLog />)
    expect(screen.getByText('FILLED')).toBeInTheDocument()
    expect(screen.getByText('BLOCKED')).toBeInTheDocument()
    expect(screen.getByText('live_trading_disabled')).toBeInTheDocument()
  })

  it('renders a dash for a missing outcome (never "orphan")', () => {
    setCtx({
      data: {
        strategy_alerts: [{ ...alerts[0], outcome: null, block_reason: null }],
        okx_executions: [],
      },
    })
    render(<StrategyAlertLog />)
    const rows = bodyRows()
    expect(rows[0].textContent).not.toContain('FILLED')
    expect(rows[0].textContent).toContain('—')
  })
})
