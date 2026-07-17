'use client'
import { ChevronDown } from 'lucide-react'

export interface SelectOption {
  value: string
  label: string
}

interface SelectProps {
  id: string
  label?: string
  value: string
  options: SelectOption[]
  onChange: (value: string) => void
}

export function Select({ id, label, value, options, onChange }: SelectProps) {
  return (
    <div className="hx-filter-bar">
      {label && (
        <label htmlFor={id} className="hx-select-label">
          {label}
        </label>
      )}
      <span className="hx-select-wrap">
        <select
          id={id}
          className="hx-select"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <ChevronDown size={12} className="hx-select-chevron" />
      </span>
    </div>
  )
}

export default Select
