// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// attendly's look: white cards on gray-50, indigo for what you act on.

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ButtonHTMLAttributes, type CSSProperties, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { LayoutGrid, MoreHorizontal, Rows3, X } from 'lucide-react'
import type { MarkStatus } from '../attendance'
import { STATUS_LABEL } from '../attendance'

type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost'

const BUTTON: Record<ButtonVariant, string> = {
  primary: 'bg-indigo-600 text-white hover:bg-indigo-700 focus-visible:ring-indigo-300 border-transparent',
  secondary: 'bg-white text-gray-700 hover:bg-gray-50 border-gray-300 focus-visible:ring-indigo-200',
  danger: 'bg-white text-red-600 hover:bg-red-50 border-red-200 focus-visible:ring-red-200',
  ghost: 'bg-transparent text-gray-600 hover:bg-gray-100 border-transparent focus-visible:ring-indigo-200',
}

export function Button({
  variant = 'primary',
  size = 'md',
  busy,
  className = '',
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; size?: 'sm' | 'md'; busy?: boolean }) {
  const pad = size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3.5 py-2 text-sm'
  return (
    <button
      type="button"
      {...rest}
      disabled={rest.disabled || busy}
      className={`inline-flex items-center justify-center gap-1.5 rounded-md border font-medium transition-colors focus:outline-none focus-visible:ring-2 disabled:cursor-not-allowed disabled:opacity-50 ${pad} ${BUTTON[variant]} ${className}`}
    >
      {busy && <Spinner className="h-3.5 w-3.5" />}
      {children}
    </button>
  )
}

export function Spinner({ className = 'h-5 w-5' }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="Loading"
      className={`inline-block animate-spin rounded-full border-2 border-current border-t-transparent ${className}`}
    />
  )
}

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-xl border border-gray-200 bg-white shadow-sm ${className}`}>{children}</div>
}

export function PageTitle({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-gray-900">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-gray-600">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

export type Tone = 'green' | 'amber' | 'red' | 'blue' | 'gray' | 'indigo'

const TONE: Record<Tone, string> = {
  green: 'bg-green-100 text-green-800',
  amber: 'bg-amber-100 text-amber-800',
  red: 'bg-red-100 text-red-800',
  blue: 'bg-blue-100 text-blue-800',
  gray: 'bg-gray-100 text-gray-700',
  indigo: 'bg-indigo-100 text-indigo-800',
}

export function Badge({ tone = 'gray', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${TONE[tone]}`}>
      {children}
    </span>
  )
}

const MARK_TONE: Record<MarkStatus, Tone> = { PRESENT: 'green', LATE: 'amber', ABSENT: 'red', EXCUSED: 'blue' }

export function MarkBadge({ status, manual }: { status: MarkStatus | null; manual?: boolean }) {
  if (!status) return <span className="text-gray-400">—</span>
  return (
    <Badge tone={MARK_TONE[status]} title={manual ? 'Set by a person' : 'From the cameras'}>
      {STATUS_LABEL[status]}
      {manual ? ' ✎' : ''}
    </Badge>
  )
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex items-start justify-between gap-3 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">
      <span>{message}</span>
      {onRetry && (
        <Button size="sm" variant="secondary" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  )
}

export function Empty({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
      {icon && <div className="text-gray-300">{icon}</div>}
      <h3 className="text-base font-medium text-gray-900">{title}</h3>
      {children && <div className="max-w-md text-sm text-gray-500">{children}</div>}
    </div>
  )
}

export function Loading() {
  return (
    <div className="flex justify-center py-12 text-indigo-600">
      <Spinner className="h-6 w-6" />
    </div>
  )
}

export function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-gray-700">{label}</span>
      {children}
      {error ? (
        <span className="mt-1 block text-xs text-red-600">{error}</span>
      ) : (
        hint && <span className="mt-1 block text-xs text-gray-500">{hint}</span>
      )}
    </label>
  )
}

export const inputClass =
  'block w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 placeholder-gray-400 focus:border-indigo-300 focus:outline-none focus:ring-2 focus:ring-indigo-200 disabled:bg-gray-50'

/** The same control, as wide as its content: for toolbars, where a
 *  full-width select would push everything else onto its own line. */
export const inlineInputClass = inputClass.replace('w-full', 'w-auto')

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide,
}: {
  title: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  wide?: boolean
}) {
  useEffect(() => {
    const key = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [onClose])
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 sm:items-center" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`w-full ${wide ? 'max-w-3xl' : 'max-w-lg'} rounded-xl bg-white shadow-xl`}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b px-5 py-3">
          <h2 className="text-base font-semibold text-gray-900">{title}</h2>
          <button className="rounded p-1 text-gray-500 hover:bg-gray-100" onClick={onClose} aria-label="Close">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="max-h-[70vh] overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="flex justify-end gap-2 border-t bg-gray-50 px-5 py-3 rounded-b-xl">{footer}</div>}
      </div>
    </div>
  )
}

export function StatTile({
  icon,
  value,
  label,
  tone,
}: {
  icon: ReactNode
  value: ReactNode
  label: string
  tone: 'blue' | 'green' | 'purple' | 'amber'
}) {
  const tones = {
    blue: 'bg-blue-50 border-blue-200 text-blue-600',
    green: 'bg-green-50 border-green-200 text-green-600',
    purple: 'bg-purple-50 border-purple-200 text-purple-600',
    amber: 'bg-amber-50 border-amber-200 text-amber-600',
  }
  return (
    <div className={`rounded-lg border p-4 ${tones[tone]}`}>
      <div className="flex items-center gap-2">
        {icon}
        <div className="text-2xl font-bold">{value}</div>
      </div>
      <div className="mt-1 text-sm text-gray-600">{label}</div>
    </div>
  )
}

/** A row's "…" menu. Drawn on the page itself, not inside the row: a
 *  table scrolls sideways in its own box, and a menu inside that box is
 *  clipped by it. Opens upwards when there is no room below. */
export function RowMenu({ label, children }: { label: string; children: (close: () => void) => ReactNode }) {
  const [at, setAt] = useState<{ top?: number; bottom?: number; right: number } | null>(null)
  const button = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const open = at !== null
  const close = useCallback(() => setAt(null), [])
  // Under the button, or above it when there is no room below.
  const place = useCallback(() => {
    const r = button.current?.getBoundingClientRect()
    if (!r) return
    const right = window.innerWidth - r.right
    setAt(window.innerHeight - r.bottom < 260 ? { bottom: window.innerHeight - r.top + 4, right } : { top: r.bottom + 4, right })
  }, [])

  useEffect(() => {
    if (!open) return
    const away = (e: PointerEvent) => {
      const t = e.target as Node
      if (!menu.current?.contains(t) && !button.current?.contains(t)) close()
    }
    const key = (e: KeyboardEvent) => e.key === 'Escape' && close()
    document.addEventListener('pointerdown', away)
    document.addEventListener('keydown', key)
    // Follows the button when the page or the table scrolls: closing
    // instead would shut it the moment a tap's own scroll settles.
    window.addEventListener('scroll', place, true)
    window.addEventListener('resize', place)
    return () => {
      document.removeEventListener('pointerdown', away)
      document.removeEventListener('keydown', key)
      window.removeEventListener('scroll', place, true)
      window.removeEventListener('resize', place)
    }
  }, [open, close, place])

  const toggle = () => (open ? close() : place())

  return (
    <>
      <button
        ref={button}
        type="button"
        onClick={toggle}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={!!at}
        className="rounded-md p-1.5 text-gray-600 hover:bg-gray-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-200"
      >
        <MoreHorizontal className="h-4 w-4" />
      </button>
      {at &&
        createPortal(
          <div
            ref={menu}
            role="menu"
            style={{ position: 'fixed', top: at.top, bottom: at.bottom, right: at.right }}
            className="z-50 w-52 overflow-hidden rounded-md border bg-white text-left shadow-lg"
          >
            {children(close)}
          </div>,
          document.body,
        )}
    </>
  )
}

export const menuItem = 'flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-gray-100'

/** The height from ``ref``'s top to the bottom of the window, less
 * ``reserve`` (for what sits under it, such as paging), as the page lays
 * out at the top. Followed as the window and the page above change. */
function useFillHeight(ref: RefObject<HTMLElement | null>, reserve: number, on: boolean): number | null {
  const [height, setHeight] = useState<number | null>(null)
  useLayoutEffect(() => {
    if (!on) return
    const fit = () => {
      const el = ref.current
      if (!el) return
      const top = el.getBoundingClientRect().top + window.scrollY
      // Never so short that a few rows are all there is room for.
      setHeight(Math.max(240, Math.floor(window.innerHeight - top - reserve - 24)))
    }
    fit()
    const watch = new ResizeObserver(fit)
    watch.observe(document.body)
    window.addEventListener('resize', fit)
    return () => {
      watch.disconnect()
      window.removeEventListener('resize', fit)
    }
  }, [ref, reserve, on])
  return height
}

/** A table that keeps its header in view: it always takes the window's
 * height below it (``fill``, the default), however few its rows, and
 * scrolls them inside it, and
 * sideways on a phone instead of the page. Printed, it is all there. */
export function Table({ children, fill = true, reserve = 0 }: { children: ReactNode; fill?: boolean; reserve?: number }) {
  const box = useRef<HTMLDivElement>(null)
  const height = useFillHeight(box, reserve, fill)
  return (
    <div
      ref={box}
      className="h-[var(--fill)] overflow-auto print:h-auto print:overflow-visible"
      style={{ '--fill': height ? `${height}px` : 'auto' } as CSSProperties}
    >
      <table
        className={
          'min-w-full divide-y divide-gray-200 text-sm ' +
          '[&_thead_th]:sticky [&_thead_th]:top-0 [&_thead_th]:z-10 [&_thead_th]:bg-gray-50 ' +
          '[&_thead_th]:shadow-[inset_0_-1px_0_#e5e7eb] [&_thead_th:first-child]:z-20'
        }
      >
        {children}
      </table>
    </div>
  )
}

export type ViewMode = 'table' | 'cards'

/** The page's choice of table or cards, remembered in this browser. */
export function useViewMode(page: string, initial: ViewMode = 'table'): [ViewMode, (m: ViewMode) => void] {
  const key = `attendance.view.${page}`
  const [mode, setMode] = useState<ViewMode>(() => {
    try {
      const saved = window.localStorage.getItem(key)
      return saved === 'table' || saved === 'cards' ? saved : initial
    } catch {
      return initial
    }
  })
  const choose = (m: ViewMode) => {
    setMode(m)
    try {
      window.localStorage.setItem(key, m)
    } catch {
      /* not remembered; still switched */
    }
  }
  return [mode, choose]
}

export function ViewSwitch({ value, onChange }: { value: ViewMode; onChange: (m: ViewMode) => void }) {
  const options = [
    { mode: 'table' as const, label: 'Table', Icon: Rows3 },
    { mode: 'cards' as const, label: 'Cards', Icon: LayoutGrid },
  ]
  return (
    <div className="inline-flex rounded-lg border bg-gray-50 p-0.5" role="group" aria-label="View">
      {options.map(({ mode, label, Icon }) => (
        <button
          key={mode}
          onClick={() => onChange(mode)}
          aria-pressed={value === mode}
          title={label}
          className={`flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm ${value === mode ? 'bg-white font-medium text-indigo-700 shadow-sm' : 'text-gray-600 hover:text-gray-900'}`}
        >
          <Icon className="h-4 w-4" />
          <span className="hidden sm:inline">{label}</span>
        </button>
      ))}
    </div>
  )
}

export const th = 'px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 whitespace-nowrap'
export const td = 'px-4 py-3 text-gray-700 align-middle'
