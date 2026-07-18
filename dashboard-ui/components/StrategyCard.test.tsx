import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { Strategy } from '../lib/types'

// Mock the API module so handleModeChange's network call is observable and
// controllable (resolve / reject / hang) without hitting a real backend.
vi.mock('../lib/api', () => ({
  setStrategyMode: vi.fn(),
}))

import { setStrategyMode } from '../lib/api'
import { StrategyCard } from './StrategyCard'

const mockSetMode = vi.mocked(setStrategyMode)

function makeStrategy(overrides: Partial<Strategy> = {}): Strategy {
  return {
    strategy_id: 'strat-1',
    asset: 'BTCUSDT',
    name: 'BTC Trend',
    effective_mode: 'demo',
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
  mockSetMode.mockReset()
  mockSetMode.mockResolvedValue(undefined)
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

describe('StrategyCard mode toggle', () => {
  it('disables the Live button and never calls setStrategyMode when liveEnabled is false', () => {
    renderCard({ liveEnabled: false })
    // Locked live button is exposed via its explanatory aria-label, not "Live".
    const live = screen.getByRole('button', {
      name: 'Live mode — requires HERMX_LIVE_TRADING=true',
    })
    expect(live).toBeDisabled()

    fireEvent.click(live)
    expect(mockSetMode).not.toHaveBeenCalled()
  })

  it('calls setStrategyMode with the strategy_id and clicked mode', async () => {
    renderCard({ strategy: makeStrategy({ strategy_id: 'strat-42' }) })
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))

    await waitFor(() => expect(mockSetMode).toHaveBeenCalledTimes(1))
    expect(mockSetMode).toHaveBeenCalledWith('strat-42', 'pause')
  })

  it('invokes onModeChange after a successful mode change', async () => {
    const onModeChange = vi.fn()
    renderCard({ onModeChange })
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))

    await waitFor(() => expect(onModeChange).toHaveBeenCalledTimes(1))
  })

  it('disables all mode buttons while a request is pending (no double-submit)', async () => {
    // A never-resolving promise keeps the component in the pending state.
    let resolve!: () => void
    mockSetMode.mockReturnValue(new Promise<void>((r) => { resolve = r }))

    renderCard({ liveEnabled: true })
    const pause = screen.getByRole('button', { name: 'Pause' })
    const demo = screen.getByRole('button', { name: 'Demo' })
    const live = screen.getByRole('button', { name: 'Live' })

    fireEvent.click(pause)

    await waitFor(() => expect(pause).toBeDisabled())
    expect(demo).toBeDisabled()
    expect(live).toBeDisabled()

    // A second click while pending must not enqueue another request.
    fireEvent.click(demo)
    expect(mockSetMode).toHaveBeenCalledTimes(1)

    resolve()
    await waitFor(() => expect(pause).not.toBeDisabled())
  })

  it('surfaces the error message and clears pending state on rejection', async () => {
    mockSetMode.mockRejectedValue(new Error('setStrategyMode 500: boom'))
    renderCard()
    const pause = screen.getByRole('button', { name: 'Pause' })
    fireEvent.click(pause)

    expect(await screen.findByText('setStrategyMode 500: boom')).toBeInTheDocument()
    // Pending cleared → buttons interactive again.
    await waitFor(() => expect(pause).not.toBeDisabled())
  })

  it('makes no call when strategy_id is missing', () => {
    renderCard({ strategy: makeStrategy({ strategy_id: undefined }) })
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    expect(mockSetMode).not.toHaveBeenCalled()
  })
})

describe('StrategyCard live-transition confirm', () => {
  it('asks for confirmation before switching into live and proceeds on accept', async () => {
    renderCard({ liveEnabled: true })
    fireEvent.click(screen.getByRole('button', { name: 'Live' }))

    expect(window.confirm).toHaveBeenCalledTimes(1)
    expect(vi.mocked(window.confirm).mock.calls[0][0]).toMatch(/LIVE/)
    await waitFor(() => expect(mockSetMode).toHaveBeenCalledWith('strat-1', 'live'))
  })

  it('makes no call when the live confirmation is dismissed', () => {
    vi.mocked(window.confirm).mockReturnValue(false)
    renderCard({ liveEnabled: true })
    fireEvent.click(screen.getByRole('button', { name: 'Live' }))

    expect(window.confirm).toHaveBeenCalledTimes(1)
    expect(mockSetMode).not.toHaveBeenCalled()
  })

  it('does not re-confirm when already live', async () => {
    renderCard({ strategy: makeStrategy({ effective_mode: 'live' }), liveEnabled: true })
    fireEvent.click(screen.getByRole('button', { name: 'Live' }))

    expect(window.confirm).not.toHaveBeenCalled()
    await waitFor(() => expect(mockSetMode).toHaveBeenCalledWith('strat-1', 'live'))
  })

  it('confirms pause when an open position exists and blocks on dismiss', () => {
    vi.mocked(window.confirm).mockReturnValue(false)
    renderCard({ position: { side: 'LONG', upl: 1.5 } })
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))

    expect(window.confirm).toHaveBeenCalledTimes(1)
    expect(vi.mocked(window.confirm).mock.calls[0][0]).toMatch(/open position/)
    expect(mockSetMode).not.toHaveBeenCalled()
  })

  it('pauses without confirm when flat', async () => {
    renderCard()
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))

    expect(window.confirm).not.toHaveBeenCalled()
    await waitFor(() => expect(mockSetMode).toHaveBeenCalledWith('strat-1', 'pause'))
  })
})

describe('StrategyCard provenance and accounting window', () => {
  it('shows "live since" when live with an override set_at', () => {
    renderCard({
      strategy: makeStrategy({ effective_mode: 'live' }),
      liveEnabled: true,
      override: { mode: 'live', set_at: new Date(Date.now() - 3600_000).toISOString() },
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
  it('groups the mode toggle with an accessible name', () => {
    renderCard()
    expect(screen.getByRole('group', { name: 'Trading mode' })).toBeInTheDocument()
  })

  it('reflects the current mode via aria-pressed', () => {
    renderCard({ strategy: makeStrategy({ effective_mode: 'demo' }), liveEnabled: true })
    expect(screen.getByRole('button', { name: 'Demo' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Pause' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: 'Live' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('exposes the lock reason on the disabled Live button via aria-label', () => {
    renderCard({ liveEnabled: false })
    expect(
      screen.getByRole('button', { name: 'Live mode — requires HERMX_LIVE_TRADING=true' }),
    ).toBeDisabled()
  })
})
