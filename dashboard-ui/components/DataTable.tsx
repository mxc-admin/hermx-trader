interface Column<T> {
  key: string
  header: string
  render: (row: T, idx: number) => React.ReactNode
  width?: string
}

interface DataTableProps<T> {
  columns: Column<T>[]
  rows: T[]
  /** Accessible name for the table, exposed via aria-label. Required for a11y. */
  label: string
  emptyMessage?: string
  maxHeight?: string
  className?: string
  /** When set, rows are clickable (pointer cursor, keyboard-activatable). */
  onRowClick?: (row: T, idx: number) => void
  /** Marks a row visually selected (used with onRowClick toggle filters). */
  rowSelected?: (row: T, idx: number) => boolean
}

export function DataTable<T>({
  columns,
  rows,
  label,
  emptyMessage = 'No data',
  maxHeight = '400px',
  className,
  onRowClick,
  rowSelected,
}: DataTableProps<T>) {
  return (
    <div
      className={className}
      style={{
        maxHeight,
        overflowY: 'auto',
        border: '1px solid var(--border-dim)',
        borderRadius: 4,
      }}
    >
      <table
        aria-label={label}
        style={{
          width: '100%',
          borderCollapse: 'collapse',
          fontFamily: 'var(--font-mono), monospace',
          fontSize: 12,
        }}
      >
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                scope="col"
                style={{
                  position: 'sticky',
                  top: 0,
                  zIndex: 1,
                  width: col.width,
                  textAlign: 'left',
                  padding: '8px 12px',
                  background: 'var(--bg-panel-raised)',
                  color: 'var(--text-muted)',
                  fontWeight: 600,
                  fontSize: 10,
                  letterSpacing: '0.08em',
                  textTransform: 'uppercase',
                  borderBottom: '1px solid var(--border-dim)',
                  whiteSpace: 'nowrap',
                }}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td
                colSpan={columns.length}
                style={{
                  padding: '24px 12px',
                  textAlign: 'center',
                  color: 'var(--text-muted)',
                }}
              >
                {emptyMessage}
              </td>
            </tr>
          ) : (
            rows.map((row, idx) => {
              const selected = rowSelected?.(row, idx) ?? false
              const baseBg = selected
                ? 'var(--bg-hover)'
                : idx % 2 === 0
                  ? 'var(--bg-panel)'
                  : 'var(--bg-panel-raised)'
              return (
              <tr
                key={idx}
                onClick={onRowClick ? () => onRowClick(row, idx) : undefined}
                onKeyDown={
                  onRowClick
                    ? (e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          onRowClick(row, idx)
                        }
                      }
                    : undefined
                }
                tabIndex={onRowClick ? 0 : undefined}
                aria-selected={onRowClick ? selected : undefined}
                style={{
                  background: baseBg,
                  cursor: onRowClick ? 'pointer' : undefined,
                  outline: selected ? '1px solid var(--border)' : undefined,
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = 'var(--bg-hover)'
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = baseBg
                }}
              >
                {columns.map((col) => (
                  <td
                    key={col.key}
                    style={{
                      padding: '8px 12px',
                      color: 'var(--text-secondary)',
                      borderBottom: '1px solid var(--border-dim)',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {col.render(row, idx)}
                  </td>
                ))}
              </tr>
              )
            })
          )}
        </tbody>
      </table>
    </div>
  )
}

export default DataTable
