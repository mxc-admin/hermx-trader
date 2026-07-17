'use client'
import { useDashboardContext } from './DashboardProvider'
import { StrategyCard } from './StrategyCard'

export function StrategyGrid() {
  const { data, health, refresh } = useDashboardContext()
  const strategies = data?.strategies ?? []
  const positions = data?.okx_live?.positions ?? {}
  const liveEnabled = health?.arm?.live_trading_enabled ?? false

  if (strategies.length === 0) {
    return (
      <div className="metric-sub" style={{ color: 'var(--text-muted)' }}>
        No active strategies
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {strategies.map((strategy, i) => (
        <StrategyCard
          key={strategy.strategy_id ?? strategy.asset ?? i}
          strategy={strategy}
          position={strategy.asset ? positions[strategy.asset] : undefined}
          liveEnabled={liveEnabled}
          onModeChange={refresh}
        />
      ))}
    </div>
  )
}

export default StrategyGrid
