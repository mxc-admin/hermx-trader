import type { ArmState } from './types'

export type SystemStatusKind = 'armed' | 'demo' | 'disarmed'

export interface SystemStatus {
  kind: SystemStatusKind
  /** health.arm.armed — live strategies exist AND the global gate is enabled. */
  armed: boolean
  killSwitchEngaged: boolean
  liveStrategies: number
  demoStrategies: number
}

// Single ARMED predicate shared by ArmingBanner and SummaryCards so the banner
// and the SYSTEM STATUS card can never disagree.
export function deriveSystemStatus(arm: ArmState | null | undefined): SystemStatus {
  const a = arm ?? {}
  const liveStrategies = a.live_strategies ?? 0
  const demoStrategies = a.demo_strategies ?? 0
  const armed = !!a.armed
  const kind: SystemStatusKind = armed
    ? 'armed'
    : liveStrategies + demoStrategies > 0
      ? 'demo'
      : 'disarmed'
  return {
    kind,
    armed,
    killSwitchEngaged: !!a.kill_switch_engaged,
    liveStrategies,
    demoStrategies,
  }
}
