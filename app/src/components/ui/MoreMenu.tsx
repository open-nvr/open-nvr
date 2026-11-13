// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * A "⋯" button that opens a short list of secondary page actions. A title
 * bar keeps its one or two everyday actions in view and folds the rest
 * here, instead of a row of equal-weight buttons that all read as primary.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { MoreHorizontal } from 'lucide-react'
import { clsx } from 'clsx'
import { useClickOutside } from '../../hooks/useClickOutside'

export interface MoreMenuItem {
  key: string
  label: ReactNode
  icon?: ReactNode
  onClick: () => void
  disabled?: boolean
  title?: string
}

export function MoreMenu({ items, label = 'More actions' }: { items: MoreMenuItem[]; label?: string }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useClickOutside(ref, open, () => setOpen(false))
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open])

  if (items.length === 0) return null
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        title={label}
        className={clsx(
          'grid h-7 w-7 place-items-center border transition-colors',
          open
            ? 'border-[var(--accent)] text-[var(--accent)]'
            : 'border-[var(--border)] text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel)]',
        )}
      >
        <MoreHorizontal size={14} />
      </button>
      {open && (
        <div role="menu" className="absolute right-0 top-full z-50 mt-1 min-w-[190px] border border-[var(--border)] bg-[var(--panel)] py-1 shadow-lg">
          {items.map((it) => (
            <button
              key={it.key}
              type="button"
              role="menuitem"
              disabled={it.disabled}
              title={it.title}
              onClick={() => { setOpen(false); it.onClick() }}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-[var(--text)] hover:bg-[var(--panel-2)] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {it.icon && <span className="flex shrink-0 text-[var(--text-dim)]">{it.icon}</span>}
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
