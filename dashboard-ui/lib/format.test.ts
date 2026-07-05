import { describe, it, expect } from 'vitest'
import { envBreakdown } from './format'

describe('envBreakdown', () => {
  it('returns null when envs is undefined or null', () => {
    expect(envBreakdown(undefined)).toBeNull()
    expect(envBreakdown(null)).toBeNull()
  })

  it('returns null for a single entry (legacy single-venue behavior)', () => {
    expect(envBreakdown({ 'okx:demo': { ok: true } })).toBeNull()
    expect(envBreakdown({ 'okx:demo': { ok: false } })).toBeNull()
  })

  it('returns "N/N venues OK" when every env is ok', () => {
    expect(
      envBreakdown({
        'okx:demo': { ok: true },
        'kucoin:live': { ok: true },
        'bybit:demo': { ok: true },
      }),
    ).toBe('3/3 venues OK')
  })

  it('counts an entry missing the ok field as non-ok', () => {
    expect(
      envBreakdown({
        'okx:demo': { ok: true },
        'kucoin:live': {},
      }),
    ).toBe('degraded: kucoin:live')
  })

  it('lists an entry as degraded when ok is true but degraded is true (stale-only)', () => {
    expect(
      envBreakdown({
        'okx:demo': { ok: true },
        'bybit:demo': { ok: true, degraded: true },
      }),
    ).toBe('degraded: bybit:demo')
  })
})
