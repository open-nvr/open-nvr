/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// A dialog opened ON TOP of the app configuration panel.
//
// Rendered into <body>, so it is never clipped by the panel's scrolling
// body. Escape closes THIS dialog only: the panel underneath listens for
// Escape on window, and catching the key on the way down (capture phase,
// on document) and stopping it there keeps one stray keypress from
// throwing away the whole unsaved form.

import { useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

export function StackedDialog({
  title, subtitle, onClose, footer, children, widthClassName = 'w-[880px]', fullHeight = false,
}: {
  title: ReactNode
  subtitle?: ReactNode
  onClose: () => void
  footer?: ReactNode
  children: ReactNode
  widthClassName?: string
  /** A fixed 90vh rather than "up to" it, so a body that fills the space
   *  (a picture scaled to fit) has a height to fill. */
  fullHeight?: boolean
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      onClose()
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [onClose])

  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div className={`relative z-10 flex ${fullHeight ? 'h-[90vh]' : 'max-h-[90vh]'} max-w-[95vw] flex-col border border-neutral-700
                       bg-[var(--panel-2)] shadow-xl ${widthClassName}`}>
        <div className="flex shrink-0 items-center gap-2 border-b border-[var(--border)] px-4 py-3">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold">{title}</h2>
            {subtitle && <div className="truncate text-xs text-[var(--text-dim)]">{subtitle}</div>}
          </div>
          <button type="button" onClick={onClose} aria-label="Close"
                  className="ml-auto rounded p-1 text-[var(--text-dim)] hover:text-[var(--text)]">
            <X size={16} />
          </button>
        </div>
        {children}
        {footer && (
          <div className="flex shrink-0 items-center gap-2 border-t border-[var(--border)] px-4 py-3">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}
