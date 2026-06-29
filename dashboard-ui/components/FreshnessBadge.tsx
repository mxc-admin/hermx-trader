'use client'
import { useDashboardContext } from './DashboardProvider'
import { Badge } from './Badge'

export function FreshnessBadge() {
  const { data } = useDashboardContext()
  const freshness = data?.freshness
  const age = freshness?.age_seconds ?? null

  if (freshness?.stale) return <Badge label="STALE" kind="bad" />
  if (age !== null && age > 30) return <Badge label={`AGE ${Math.round(age)}s`} kind="warn" />
  return <Badge label="LIVE" kind="good" />
}

export default FreshnessBadge
