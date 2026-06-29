'use client'
import { useDashboardContext } from './DashboardProvider'
import { StatCard } from './StatCard'

// Surfaces the explicit executor-read verdict (src/dashboard.py:
// executor_health_summary) so an errored/stale read never reads as healthy.
export function ExecutorHealthCard() {
  const { data } = useDashboardContext()
  const executor = data?.executor ?? {}
  const ledgerHealth = data?.ledger_health ?? {}

  // ok / stale / error precedence: an error wins over staleness.
  let value: string
  let accentColor: string
  if (!executor.healthy) {
    value = 'ERROR'
    accentColor = 'var(--negative)'
  } else if (executor.stale) {
    value = 'STALE'
    accentColor = 'var(--warning)'
  } else {
    value = 'OK'
    accentColor = 'var(--positive)'
  }

  const sub = executor.error
    ? executor.error.slice(0, 50)
    : 'executor healthy'

  const skipped = ledgerHealth.total_skipped ?? 0

  return (
    <div>
      <StatCard
        label="EXECUTOR"
        value={value}
        sub={sub}
        accentColor={accentColor}
        valueColor={accentColor}
      />
      {skipped > 0 && (
        <div
          className="metric-sub"
          style={{ marginTop: 6, color: 'var(--warning)' }}
        >
          {skipped} corrupt ledger {skipped === 1 ? 'line' : 'lines'} skipped
        </div>
      )}
    </div>
  )
}

export default ExecutorHealthCard
