'use client'
import { useDashboardContext } from './DashboardProvider'
import { StrategyCard } from './StrategyCard'

export function StrategyGrid() {
  const { data, health, refresh } = useDashboardContext()
  const strategies = data?.strategies ?? []
  const positions = data?.okx_live?.positions ?? {}
  const alerts = data?.strategy_alerts ?? []
  const liveEnabled = health?.arm?.live_trading_enabled ?? false

  if (strategies.length === 0) {
    return (
      <div className="metric-sub" style={{ color: 'var(--text-muted)' }}>
        No active strategies
      </div>
    )
  }

  // Exact strategy_id match when both sides carry one; asset is only a last
  // resort so two strategies sharing an asset never cross-count alerts.
  const alertCount = (strategy: { strategy_id?: string; asset?: string }) =>
    alerts.filter((a) =>
      strategy.strategy_id && a.strategy_id
        ? a.strategy_id === strategy.strategy_id
        : Boolean(strategy.asset && a.asset === strategy.asset),
    ).length

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {strategies.map((strategy, i) => (
        <StrategyCard
          key={strategy.strategy_id ?? strategy.asset ?? i}
          strategy={strategy}
          position={strategy.asset ? positions[strategy.asset] : undefined}
          alertCount={alertCount(strategy)}
          liveEnabled={liveEnabled}
          onModeChange={refresh}
        />
      ))}
    </div>
  )
}

export default StrategyGrid
