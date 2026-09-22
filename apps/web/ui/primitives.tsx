import { useEffect, useId, useRef, type ButtonHTMLAttributes, type ReactNode } from 'react'
import { Search, X } from 'lucide-react'
import { createDropdown } from '../shared/dropdown.js'

export function Button({
  variant = 'ghost',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'ghost' | 'danger' }) {
  return (
    <button type="button" className={`ui-button ui-button--${variant} ${className}`} {...props} />
  )
}
export function IconButton({
  label,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return <Button className="ui-icon-button" aria-label={label} title={label} {...props} />
}
export function SectionHeader({
  title,
  badge,
  action,
}: {
  title: string
  badge?: string
  action?: ReactNode
}) {
  return (
    <div className="section-header">
      <div className="section-title">
        <h2>{title}</h2>
        {badge && <span className="ui-badge">{badge}</span>}
      </div>
      {action}
    </div>
  )
}
export function Tabs<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled = false,
}: {
  label: string
  value: T
  options: readonly { value: T; label: string }[]
  onChange: (value: T) => void
  disabled?: boolean
}) {
  // A single shared content region: pressed buttons avoid inventing inaccessible tab panels.
  return (
    <div className="ui-tabs" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          type="button"
          key={option.value}
          disabled={disabled}
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
export function Select({
  label,
  value,
  options,
  onChange,
  disabled = false,
}: {
  label: string
  value: string
  options: readonly { value: string; label: string }[]
  onChange: (value: string) => void
  disabled?: boolean
}) {
  const ref = useRef<HTMLSelectElement>(null)
  const adapter = useRef<ReturnType<typeof createDropdown> | null>(null)
  useEffect(() => {
    if (!ref.current) {
      return
    }
    adapter.current = createDropdown(ref.current)
    // Native events emitted by the existing dropdown are bridged explicitly to React.
    const change = () => onChangeRef.current(ref.current!.value)
    ref.current.addEventListener('change', change)
    const select = ref.current
    return () => {
      select.removeEventListener('change', change)
      adapter.current?.dispose()
    }
  }, [])
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  useEffect(() => {
    adapter.current?.sync()
  }, [value, disabled, options])
  return (
    <span className="ui-select">
      <select ref={ref} aria-label={label} value={value} disabled={disabled} onChange={() => {}}>
        {options.map((option) => (
          <option value={option.value} key={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </span>
  )
}
export function SearchInput({
  value,
  onChange,
  label = '搜索灵感',
}: {
  value: string
  onChange: (value: string) => void
  label?: string
}) {
  return (
    <label className="ui-search">
      <Search size={17} aria-hidden="true" />
      <input
        type="search"
        aria-label={label}
        placeholder={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  )
}
export function Dialog({
  title,
  children,
  onClose,
}: {
  title: string
  children: ReactNode
  onClose: () => void
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const id = useId()
  useEffect(() => {
    const dialog = ref.current!
    dialog.showModal()
    return () => dialog.close()
  }, [])
  return (
    <dialog
      ref={ref}
      className="ui-dialog"
      aria-labelledby={id}
      onCancel={(event) => {
        event.preventDefault()
        onClose()
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          onClose()
        }
      }}
    >
      <div className="dialog-heading">
        <h2 id={id}>{title}</h2>
        <IconButton label="关闭" onClick={onClose}>
          <X size={20} />
        </IconButton>
      </div>
      {children}
    </dialog>
  )
}
export function EmptyState({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty-state">
      <p>{children}</p>
      {action}
    </div>
  )
}
