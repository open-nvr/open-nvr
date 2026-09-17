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

import { ReactNode, useEffect } from 'react'
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
}

export function Modal({
  open, title, onClose, children, widthClassName, footer, bodyClassName,
  placement = 'center',
}: ModalProps) {
  const { t } = useTranslation()

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    if (open) window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  const side = placement === 'side'
  return (
    <div className={`fixed inset-0 z-50 flex ${
      side ? 'justify-end' : 'items-center justify-center'}`}>
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      {/* Dialog. flex-col + min-h-0 so content taller than the box
          scrolls in the body instead of being clipped by
          overflow-hidden. A side panel is full-height and bounded by
          the viewport width, so it never runs off a laptop screen. */}
      <div className={`relative z-10 flex flex-col overflow-hidden border-neutral-700 bg-[var(--panel-2)] shadow-xl ${
        side
          ? `h-full max-w-[95vw] border-l ${widthClassName || 'w-[760px]'}`
          : `max-h-[85vh] border ${widthClassName || 'w-[720px]'}`}`}>
        <div className="flex items-center justify-between gap-2 border-b border-neutral-700 px-4 py-2">
          <h2 className="text-sm font-semibold flex items-center gap-2">{title}</h2>
          <button className="text-[var(--text-dim)] hover:text-white" onClick={onClose} aria-label={t('shared.close')}>✕</button>
        </div>
        <div className={`flex-1 min-h-0 overflow-auto thin-scroll ${bodyClassName || 'p-4'}`}>
          {children}
        </div>
        {footer && (
          <div className="border-t border-neutral-700 px-4 py-3">
            {footer}
          </div>
        )}
      </div>
    </div>
  )
}
