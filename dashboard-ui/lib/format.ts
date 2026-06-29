// Display formatters. Every helper renders an em-dash ("—") for missing values
// so the UI never shows "null", "NaN", or "undefined".

const DASH = '—'

function toNumber(val: number | string | null | undefined): number | null {
  if (val === null || val === undefined || val === '') return null
  const n = typeof val === 'string' ? Number(val) : val
  return Number.isFinite(n) ? n : null
}

/** "$1,234.56" — em-dash for null/undefined. */
export function money(val: number | null | undefined, decimals = 2): string {
  const n = toNumber(val)
  if (n === null) return DASH
  return n.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}

/** "+2.3%" — signed, em-dash for null/undefined. Input is already a percent. */
export function pct(val: number | null | undefined, decimals = 1): string {
  const n = toNumber(val)
  if (n === null) return DASH
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(decimals)}%`
}

/** Relative age of an ISO timestamp: "just now", "5s ago", "2m ago", "3h ago". */
export function age(isoString: string | null | undefined): string {
  if (!isoString) return DASH
  const then = Date.parse(isoString)
  if (Number.isNaN(then)) return DASH
  const seconds = Math.floor((Date.now() - then) / 1000)
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

/** CSS color var for a position side. */
export function sideColor(side: string): string {
  switch ((side || '').toUpperCase()) {
    case 'LONG':
      return 'var(--positive)'
    case 'SHORT':
      return 'var(--negative)'
    default:
      return 'var(--text-muted)'
  }
}

/** Badge kind for a position side. */
export function sideKind(side: string): 'good' | 'bad' | 'muted' {
  switch ((side || '').toUpperCase()) {
    case 'LONG':
      return 'good'
    case 'SHORT':
      return 'bad'
    default:
      return 'muted'
  }
}

/** Plain fixed-decimal number — em-dash for null/undefined/non-numeric. */
export function num(
  val: number | string | null | undefined,
  decimals = 2,
): string {
  const n = toNumber(val)
  if (n === null) return DASH
  return n.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}
