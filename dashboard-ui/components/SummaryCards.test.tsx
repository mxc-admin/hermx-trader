import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

// Mock the context hook: SummaryCards only reads data + health from it.
vi.mock('./DashboardProvider', () => ({
  useDashboardContext: vi.fn(),
}))

import { useDashboardContext } from './DashboardProvider'
import { SummaryCards } from './SummaryCards'

const mockCtx = vi.mocked(useDashboardContext)

function setCtx(data: Record<string, unknown>) {
  mockCtx.mockReturnValue({
    data,
    health: null,
  } as never)
}

const card = (label: string) => {
  const el = screen.getByText(label).closest('.metric-card')
  if (!el) throw new Error(`card ${label} not found`)
  return el
}

beforeEach(() => {
  mockCtx.mockReset()
})

describe('SummaryCards open positions', () => {
  it('counts positions.open across venues (HL-only short, empty okx_live)', () => {
    setCtx({
      strategies: [],
      okx_live: { positions: {} },
      positions: {
        open: [
          {
            status: 'open',
            venue: 'hyperliquid',
            mode: 'live',
            inst_id: 'BTC-USDT-SWAP',
            side: 'short',
            qty: 0.05,
          },
        ],
      },
    })
    render(<SummaryCards />)
    const pos = card('OPEN POSITIONS')
    expect(pos.textContent).toContain('1')
    expect(pos.textContent).toContain('0L / 1S')
  })

  it('shows zero when the positions contract is present but empty (no okx_live bleed-through)', () => {
    setCtx({
      strategies: [],
      okx_live: { positions: { BTCUSDT: { side: 'LONG', pos: 1 } } },
      positions: { open: [] },
    })
    render(<SummaryCards />)
    const pos = card('OPEN POSITIONS')
    expect(pos.textContent).toContain('0L / 0S')
  })

  it('falls back to okx_live.positions for old payloads without the contract', () => {
    setCtx({
      strategies: [],
      okx_live: {
        positions: {
          BTCUSDT: { side: 'LONG', pos: 1 },
          ETHUSDT: { side: 'FLAT', pos: 0 },
        },
      },
    })
    render(<SummaryCards />)
    const pos = card('OPEN POSITIONS')
    expect(pos.textContent).toContain('1')
    expect(pos.textContent).toContain('1L / 0S')
  })
})
