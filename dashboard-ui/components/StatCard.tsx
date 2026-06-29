import type { CSSProperties } from 'react'

interface StatCardProps {
  label: string
  value: string
  sub?: string
  accentColor?: string // CSS color string, used for border-top 3px
  valueColor?: string // CSS color for the value text
}

export function StatCard({ label, value, sub, accentColor, valueColor }: StatCardProps) {
  const cardStyle: CSSProperties = accentColor
    ? { borderTop: `3px solid ${accentColor}` }
    : {}

  return (
    <div className="metric-card" style={cardStyle}>
      <div className="metric-label">{label}</div>
      <div className="metric-value" style={valueColor ? { color: valueColor } : undefined}>
        {value}
      </div>
      {sub !== undefined && <div className="metric-sub">{sub}</div>}
    </div>
  )
}

export default StatCard
