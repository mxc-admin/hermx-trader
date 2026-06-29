'use client'
import { useDashboardContext } from './DashboardProvider'
import { StrategyCard } from './StrategyCard'

// Responsive grid of strategy cards. Positions come from okx_live.positions
// keyed by symbol; alert counts are tallied from strategy_alerts.
export function StrategyGrid() {
  const { data } = useDashboardContext()
  const strategies = data?.strategies ?? []
  const positions = data?.okx_live?.positions ?? {}
  const alerts = data?.strategy_alerts ?? []

  if (strategies.length === 0) {
    return (
      <div className="metric-sub" style={{ color: 'var(--text-muted)' }}>
        No active strategies
      </div>
    )
  }

  const alertCount = (strategy: { strategy_id?: string; asset?: string }) =>
    alerts.filter(
      (a) =>
        (strategy.strategy_id && a.strategy_id === strategy.strategy_id) ||
        (strategy.asset && a.asset === strategy.asset),
    ).length

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {strategies.map((strategy, i) => (
        <StrategyCard
          key={strategy.strategy_id ?? strategy.asset ?? i}
          strategy={strategy}
          position={strategy.asset ? positions[strategy.asset] : undefined}
          alertCount={alertCount(strategy)}
        />
      ))}
    </div>
  )
}

export default StrategyGrid
