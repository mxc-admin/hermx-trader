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
      alertCount={props.alertCount ?? 0}
      liveEnabled={props.liveEnabled ?? false}
      onModeChange={props.onModeChange}
    />,
  )
}

beforeEach(() => {
  mockSetMode.mockReset()
  mockSetMode.mockResolvedValue(undefined)
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
