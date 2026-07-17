'use client'
import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

interface SectionProps {
  title: string
  children: React.ReactNode
  defaultOpen?: boolean
  className?: string
  actions?: React.ReactNode
}

export function Section({ title, children, defaultOpen = true, className, actions }: SectionProps) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <section className={className}>
      <div className="section-header-row">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="section-header"
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            flex: 1,
            background: 'transparent',
            border: 'none',
            marginBottom: 0,
            cursor: 'pointer',
            textAlign: 'left',
          }}
        >
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          {title}
        </button>
        {actions}
      </div>
      {open && children}
    </section>
  )
}

export default Section
