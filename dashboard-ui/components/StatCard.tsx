import type { CSSProperties } from 'react'

interface StatCardProps {
  label: string
  value: string
  value2?: string
  sub?: string
  accentColor?: string // CSS color string, used for border-top 3px
  valueColor?: string // CSS color for the value text
  value2Color?: string
}

export function StatCard({ label, value, value2, sub, accentColor, valueColor, value2Color }: StatCardProps) {
  const cardStyle: CSSProperties = accentColor
    ? { borderTop: `3px solid ${accentColor}` }
    : {}

  return (
    <div className="metric-card" style={cardStyle}>
      <div className="metric-label">{label}</div>
      <div className="metric-value" style={valueColor ? { color: valueColor } : undefined}>
        {value}
      </div>
      {value2 !== undefined && (
        <div className="metric-value" style={value2Color ? { color: value2Color } : undefined}>
          {value2}
        </div>
      )}
      {sub !== undefined && <div className="metric-sub">{sub}</div>}
    </div>
  )
}

export default StatCard
