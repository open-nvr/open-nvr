/**
 * Copyright (c) 2026 OpenNVR
 * This file is part of OpenNVR.
 * 
 * OpenNVR is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 * 
 * OpenNVR is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 * 
 * You should have received a copy of the GNU Affero General Public License
 * along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.
 */

import { ReactNode, useEffect, useId, useRef } from 'react'
import { useTranslation } from '../i18n'

type ModalProps = {
  open: boolean
  title?: ReactNode
  onClose: () => void
  children: ReactNode
  widthClassName?: string
  /** Rendered in a border-t bar pinned below the scrolling body. */
  footer?: ReactNode
  /** Overrides the body's default `p-4` (e.g. `p-0` for edge-to-edge tabs). */
  bodyClassName?: string
  /**
   * Where the dialog sits.
   *
   * `center` (the default) is the right shape for a decision — confirm,
   * rename, pick one thing — where the page behind is irrelevant until
   * you answer.
   *
   * `side` is for WORK: a long form you fill in against the page you
   * came from. It takes the full height of the window, so a tall form
   * scrolls once instead of inside an 85vh box, and it leaves the
   * screen it belongs to visible beside it.
   *
   * A variant here rather than a second component: these two share the
   * Escape key, the backdrop click, the header and the footer bar, and
   * a separate Drawer would have drifted from the Modal within a
   * release.
   */
  placement?: 'center' | 'side'
  /** Passed to the dialog element, for tests. */
  'data-testid'?: string
  /**
   * False for forms: a stray click beside a half-filled form must not throw
   * the typing away. Escape and the close button still close it.
   */
  closeOnBackdrop?: boolean
}

// Open modals, innermost last. Escape and the Tab trap act only for the top
// one: before this, every open Modal listened on window, so Escape in a
// dialog opened from another dialog closed both.
const openStack: symbol[] = []

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export function Modal({
  open, title, onClose, children, widthClassName, footer, bodyClassName,
  placement = 'center', 'data-testid': testId, closeOnBackdrop = true,
}: ModalProps) {
  const { t } = useTranslation()
  const titleId = useId()
  const dialogRef = useRef<HTMLDivElement>(null)
  // Callers pass inline arrow functions; reading through a ref keeps the
  // effect below from re-running (and re-grabbing focus) on every render.
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    if (!open) return
    const id = Symbol('modal')
    openStack.push(id)
    const opener = document.activeElement as HTMLElement | null

    // Move focus into the dialog -- unless something inside it already took
    // it (an autoFocus input mounts before this effect runs).
    const dialog = dialogRef.current
    if (dialog && !dialog.contains(document.activeElement)) dialog.focus()

    function onKey(e: KeyboardEvent) {
      if (openStack[openStack.length - 1] !== id) return
      if (e.key === 'Escape') {
        onCloseRef.current()
        return
      }
      if (e.key !== 'Tab' || !dialog) return
      // Only trap focus that is ours. Focus sitting elsewhere (a stacked
      // picker rendered outside this dialog, a video in fullscreen) belongs
      // to another layer, and pulling it back would break that layer.
      const active = document.activeElement
      if (active && active !== document.body && !dialog.contains(active)) return
      const items = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.offsetParent !== null)
      if (items.length === 0) {
        e.preventDefault()
        dialog.focus()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      if (e.shiftKey && (active === first || active === dialog)) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      const i = openStack.indexOf(id)
      if (i >= 0) openStack.splice(i, 1)
      // Give focus back to what opened the dialog, if it is still there.
      if (opener && opener.isConnected) opener.focus()
    }
  }, [open])

  if (!open) return null
  const side = placement === 'side'
  return (
    <div className={`fixed inset-0 z-50 flex ${
      side ? 'justify-end' : 'items-center justify-center'}`}>
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60" onClick={closeOnBackdrop ? onClose : undefined} />
      {/* Dialog. flex-col + min-h-0 so content taller than the box
          scrolls in the body instead of being clipped by
          overflow-hidden. A side panel is full-height and bounded by
          the viewport width, so it never runs off a laptop screen. */}
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        tabIndex={-1}
        data-testid={testId}
        className={`relative z-10 flex flex-col overflow-hidden border-[var(--border)] bg-[var(--panel-2)] shadow-xl outline-none ${
        side
          ? `h-full max-w-[95vw] border-l ${widthClassName || 'w-[760px]'}`
          : `max-h-[85vh] border ${widthClassName || 'w-[720px]'}`}`}>
        <div className="flex items-center justify-between gap-2 border-b border-[var(--border)] px-4 py-2">
          <h2 id={titleId} className="text-sm font-semibold flex items-center gap-2">{title}</h2>
          <button className="grid h-8 w-8 place-items-center text-[var(--text-dim)] hover:bg-[var(--bg-2)] hover:text-[var(--text)]" onClick={onClose} aria-label={t('shared.close')} title={t('shared.close')}>✕</button>
        </div>
        <div className={`flex-1 min-h-0 overflow-auto thin-scroll ${bodyClassName || 'p-4'}`}>
          {children}
        </div>
        {footer && (
          <div className="border-t border-[var(--border)] px-4 py-3">
            {footer}
          </div>
        )}
      </div>
    </div>
  )
}
