interface Column<T> {
  key: string
  header: string
  render: (row: T, idx: number) => React.ReactNode
  width?: string
}

interface DataTableProps<T> {
  columns: Column<T>[]
  rows: T[]
  emptyMessage?: string
  maxHeight?: string
  className?: string
}

export function DataTable<T>({
  columns,
  rows,
  emptyMessage = 'No data',
  maxHeight = '400px',
  className,
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
            rows.map((row, idx) => (
              <tr
                key={idx}
                style={{
                  background:
                    idx % 2 === 0 ? 'var(--bg-panel)' : 'var(--bg-panel-raised)',
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = 'var(--bg-hover)'
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background =
                    idx % 2 === 0 ? 'var(--bg-panel)' : 'var(--bg-panel-raised)'
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
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}

export default DataTable
