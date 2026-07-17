'use client'
import { useDashboardContext } from './DashboardProvider'
import { Select } from './Select'

export function StrategyFilter() {
  const { data, strategyFilter, setStrategyFilter } = useDashboardContext()
  const strategyIds = (data?.strategies ?? [])
    .map((s) => s.strategy_id)
    .filter((sid): sid is string => Boolean(sid))

  return (
    <Select
      id="strategy-filter"
      label="Strategy"
      value={strategyFilter ?? ''}
      onChange={(v) => setStrategyFilter(v || null)}
      options={[
        { value: '', label: 'All strategies' },
        ...strategyIds.map((sid) => ({ value: sid, label: sid })),
      ]}
    />
  )
}

export default StrategyFilter
