import type { CSSProperties } from 'react'

type BadgeKind = 'good' | 'bad' | 'warn' | 'neutral' | 'muted' | 'info'

interface BadgeProps {
  label: string
  kind?: BadgeKind
  className?: string
}

// Colors live in globals.css as CSS variables, not in the Tailwind theme, so we
// build the palette with inline styles + color-mix() tints rather than classes.
const STYLES: Record<BadgeKind, CSSProperties> = {
  good: {
    background: 'color-mix(in srgb, var(--positive) 14%, transparent)',
    color: 'var(--positive)',
    border: '1px solid color-mix(in srgb, var(--positive) 30%, transparent)',
  },
  bad: {
    background: 'color-mix(in srgb, var(--negative) 14%, transparent)',
    color: 'var(--negative)',
    border: '1px solid color-mix(in srgb, var(--negative) 30%, transparent)',
  },
  warn: {
    background: 'color-mix(in srgb, var(--warning) 14%, transparent)',
    color: 'var(--warning)',
    border: '1px solid color-mix(in srgb, var(--warning) 30%, transparent)',
  },
  neutral: {
    background: 'var(--bg-panel-raised)',
    color: 'var(--text-secondary)',
    border: '1px solid var(--border-dim)',
  },
  muted: {
    background: 'transparent',
    color: 'var(--text-muted)',
    border: '1px solid transparent',
  },
  info: {
    background: 'color-mix(in srgb, var(--info) 14%, transparent)',
    color: 'var(--info)',
    border: '1px solid color-mix(in srgb, var(--info) 30%, transparent)',
  },
}

export function Badge({ label, kind = 'neutral', className }: BadgeProps) {
  return (
    <span
      className={className}
      style={{
        ...STYLES[kind],
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        padding: '2px 6px',
        borderRadius: 3,
        fontFamily: 'var(--font-mono), monospace',
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        lineHeight: 1.2,
        whiteSpace: 'nowrap',
      }}
    >
      {label}
    </span>
  )
}

export default Badge
