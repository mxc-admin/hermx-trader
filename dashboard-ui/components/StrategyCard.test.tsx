import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { Strategy } from '../lib/types'

// Mock the API module so the control handlers' network calls are observable and
// controllable (resolve / reject / hang) without hitting a real backend.
vi.mock('../lib/api', () => ({
  setStrategyRisk: vi.fn(),
  setStrategyAccount: vi.fn(),
}))

import { setStrategyRisk, setStrategyAccount } from '../lib/api'
import { StrategyCard } from './StrategyCard'

const mockSetRisk = vi.mocked(setStrategyRisk)
const mockSetAccount = vi.mocked(setStrategyAccount)

function makeStrategy(overrides: Partial<Strategy> = {}): Strategy {
  return {
    strategy_id: 'strat-1',
    asset: 'BTCUSDT',
    name: 'BTC Trend',
    effective_mode: 'demo',
    okx_account_source: 'demo',
    ...overrides,
  }
}

function renderCard(props: Partial<Parameters<typeof StrategyCard>[0]> = {}) {
  return render(
    <StrategyCard
      strategy={props.strategy ?? makeStrategy()}
      position={props.position}
      liveEnabled={props.liveEnabled ?? false}
      override={props.override}
      onModeChange={props.onModeChange}
    />,
  )
}

beforeEach(() => {
  mockSetRisk.mockReset()
  mockSetRisk.mockResolvedValue(undefined)
  mockSetAccount.mockReset()
  mockSetAccount.mockResolvedValue(undefined)
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

describe('StrategyCard split controls', () => {
  it('disables the Live account button and never calls the API when liveEnabled is false', () => {
    renderCard({ liveEnabled: false })
    const live = screen.getByRole('button', {
      name: 'Live account — requires HERMX_LIVE_TRADING=true',
    })
    expect(live).toBeDisabled()

    fireEvent.click(live)
    expect(mockSetAccount).not.toHaveBeenCalled()
  })

  it('calls setStrategyRisk with the strategy_id and clicked risk state', async () => {
    renderCard({ strategy: makeStrategy({ strategy_id: 'strat-42' }) })
    fireEvent.click(screen.getByRole('button', { name: 'Reduce' }))

    await waitFor(() => expect(mockSetRisk).toHaveBeenCalledTimes(1))
    expect(mockSetRisk).toHaveBeenCalledWith('strat-42', 'reduce')
    expect(mockSetAccount).not.toHaveBeenCalled() // risk never touches the account
  })

  it('calls setStrategyAccount when flipping demo -> live', async () => {
    renderCard({ liveEnabled: true })
    fireEvent.click(screen.getByRole('button', { name: 'Live' }))

    await waitFor(() => expect(mockSetAccount).toHaveBeenCalledTimes(1))
    expect(mockSetAccount).toHaveBeenCalledWith('strat-1', 'live')
    expect(mockSetRisk).not.toHaveBeenCalled() // account never touches risk
  })

  it('invokes onModeChange after a successful control change', async () => {
    const onModeChange = vi.fn()
    renderCard({ onModeChange })
    fireEvent.click(screen.getByRole('button', { name: 'Reduce' }))

    await waitFor(() => expect(onModeChange).toHaveBeenCalledTimes(1))
  })

  it('disables all control buttons while a request is pending (no double-submit)', async () => {
    // A never-resolving promise keeps the component in the pending state.
    let resolve!: () => void
    mockSetRisk.mockReturnValue(new Promise<void>((r) => { resolve = r }))

    renderCard({ liveEnabled: true })
    const reduce = screen.getByRole('button', { name: 'Reduce' })
    const demo = screen.getByRole('button', { name: 'Demo' })
    const live = screen.getByRole('button', { name: 'Live' })

    fireEvent.click(reduce)

    await waitFor(() => expect(reduce).toBeDisabled())
    expect(demo).toBeDisabled()
    expect(live).toBeDisabled()

    // A second click while pending must not enqueue another request.
    fireEvent.click(live)
    expect(mockSetAccount).not.toHaveBeenCalled()
    expect(mockSetRisk).toHaveBeenCalledTimes(1)

    resolve()
    await waitFor(() => expect(reduce).not.toBeDisabled())
  })

  it('surfaces the error message and clears pending state on rejection', async () => {
    mockSetRisk.mockRejectedValue(new Error('setStrategyRisk 500: boom'))
    renderCard()
    const reduce = screen.getByRole('button', { name: 'Reduce' })
    fireEvent.click(reduce)

    expect(await screen.findByText('setStrategyRisk 500: boom')).toBeInTheDocument()
    // Pending cleared → buttons interactive again.
    await waitFor(() => expect(reduce).not.toBeDisabled())
  })

  it('makes no call when strategy_id is missing', () => {
    renderCard({ strategy: makeStrategy({ strategy_id: undefined }) })
    fireEvent.click(screen.getByRole('button', { name: 'Reduce' }))
    expect(mockSetRisk).not.toHaveBeenCalled()
  })

  it('makes no call when the clicked state is already current', () => {
    renderCard({ override: { risk_state: 'reduce' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reduce' }))
    fireEvent.click(screen.getByRole('button', { name: 'Demo' }))
    expect(mockSetRisk).not.toHaveBeenCalled()
    expect(mockSetAccount).not.toHaveBeenCalled()
  })
})

describe('StrategyCard confirms', () => {
  it('asks for confirmation before switching to the live account and proceeds on accept', async () => {
    renderCard({ liveEnabled: true })
    fireEvent.click(screen.getByRole('button', { name: 'Live' }))

    expect(window.confirm).toHaveBeenCalledTimes(1)
    expect(vi.mocked(window.confirm).mock.calls[0][0]).toMatch(/LIVE/)
    await waitFor(() => expect(mockSetAccount).toHaveBeenCalledWith('strat-1', 'live'))
  })

  it('makes no call when the live confirmation is dismissed', () => {
    vi.mocked(window.confirm).mockReturnValue(false)
    renderCard({ liveEnabled: true })
    fireEvent.click(screen.getByRole('button', { name: 'Live' }))

    expect(window.confirm).toHaveBeenCalledTimes(1)
    expect(mockSetAccount).not.toHaveBeenCalled()
  })

  it('confirms an account flip when an open position exists and blocks on dismiss', () => {
    vi.mocked(window.confirm).mockReturnValue(false)
    renderCard({
      strategy: makeStrategy({ okx_account_source: 'live' }),
      override: { execution_mode: 'live' },
      liveEnabled: true,
      position: { side: 'LONG', upl: 1.5 },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Demo' }))

    expect(window.confirm).toHaveBeenCalledTimes(1)
    expect(vi.mocked(window.confirm).mock.calls[0][0]).toMatch(/does NOT move/)
    expect(mockSetAccount).not.toHaveBeenCalled()
  })

  it('confirms risk reduce when an open position exists and blocks on dismiss', () => {
    vi.mocked(window.confirm).mockReturnValue(false)
    renderCard({ position: { side: 'LONG', upl: 1.5 } })
    fireEvent.click(screen.getByRole('button', { name: 'Reduce' }))

    expect(window.confirm).toHaveBeenCalledTimes(1)
    expect(vi.mocked(window.confirm).mock.calls[0][0]).toMatch(/open position/)
    expect(mockSetRisk).not.toHaveBeenCalled()
  })

  it('reduces without confirm when flat', async () => {
    renderCard()
    fireEvent.click(screen.getByRole('button', { name: 'Reduce' }))

    expect(window.confirm).not.toHaveBeenCalled()
    await waitFor(() => expect(mockSetRisk).toHaveBeenCalledWith('strat-1', 'reduce'))
  })
})

describe('StrategyCard state derivation', () => {
  it('shows Reduce pressed when the override carries risk_state=reduce', () => {
    renderCard({ override: { risk_state: 'reduce', execution_mode: 'live' }, liveEnabled: true })
    expect(screen.getByRole('button', { name: 'Reduce' })).toHaveAttribute('aria-pressed', 'true')
    // The landmine, UI edition: risk reduce must NOT drop the account to demo.
    expect(screen.getByRole('button', { name: 'Live' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('maps a legacy "pause" effective_mode (no risk_state) to Reduce', () => {
    renderCard({ strategy: makeStrategy({ effective_mode: 'pause' }) })
    expect(screen.getByRole('button', { name: 'Reduce' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('derives the account from okx_account_source when no override exists', () => {
    renderCard({
      strategy: makeStrategy({ effective_mode: 'pause', okx_account_source: 'live' }),
      liveEnabled: true,
    })
    expect(screen.getByRole('button', { name: 'Live' })).toHaveAttribute('aria-pressed', 'true')
  })
})

describe('StrategyCard provenance and accounting window', () => {
  it('shows "live since" when the account is live with an override set_at', () => {
    renderCard({
      strategy: makeStrategy({ effective_mode: 'live', okx_account_source: 'live' }),
      liveEnabled: true,
      override: { mode: 'live', execution_mode: 'live', set_at: new Date(Date.now() - 3600_000).toISOString() },
    })
    expect(screen.getByText(/live since 1h ago/)).toBeInTheDocument()
  })

  it('shows the P&L-since caption when accounting_start_at is set', () => {
    renderCard({
      strategy: makeStrategy({ accounting_start_at: Date.parse('2026-07-01T00:00:00Z') }),
    })
    expect(screen.getByText('P&L since 2026-07-01')).toBeInTheDocument()
  })

  it('omits the P&L-since caption when accounting_start_at is null', () => {
    renderCard({ strategy: makeStrategy({ accounting_start_at: null }) })
    expect(screen.queryByText(/P&L since/)).not.toBeInTheDocument()
  })
})

describe('StrategyCard accessibility', () => {
  it('groups both controls with accessible names', () => {
    renderCard()
    expect(screen.getByRole('group', { name: 'Risk state' })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Account' })).toBeInTheDocument()
  })

  it('reflects the current states via aria-pressed', () => {
    renderCard({ liveEnabled: true })
    expect(screen.getByRole('button', { name: 'Active' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Reduce' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: 'Demo' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Live' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('exposes the lock reason on the disabled Live button via aria-label', () => {
    renderCard({ liveEnabled: false })
    expect(
      screen.getByRole('button', { name: 'Live account — requires HERMX_LIVE_TRADING=true' }),
    ).toBeDisabled()
  })
})
