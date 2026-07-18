import { describe, it, expect } from 'vitest'
import { deriveSystemStatus } from './systemStatus'

describe('deriveSystemStatus', () => {
  it('is armed only when health.arm.armed is true', () => {
    const s = deriveSystemStatus({
      armed: true,
      live_strategies: 2,
      demo_strategies: 1,
      kill_switch_engaged: false,
    })
    expect(s.kind).toBe('armed')
    expect(s.armed).toBe(true)
    expect(s.killSwitchEngaged).toBe(false)
  })

  it('is demo when strategies exist but not armed (kill switch engaged)', () => {
    const s = deriveSystemStatus({
      armed: false,
      live_strategies: 1,
      demo_strategies: 2,
      kill_switch_engaged: true,
    })
    expect(s.kind).toBe('demo')
    expect(s.killSwitchEngaged).toBe(true)
  })

  it('is disarmed with zero strategies', () => {
    expect(deriveSystemStatus({ armed: false, live_strategies: 0, demo_strategies: 0 }).kind).toBe(
      'disarmed',
    )
  })

  it('treats missing arm as disarmed, never armed', () => {
    const s = deriveSystemStatus(undefined)
    expect(s.kind).toBe('disarmed')
    expect(s.armed).toBe(false)
  })
})
