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

type ModalProps = {
  open: boolean
  title?: string
  onClose: () => void
  children: ReactNode
  widthClassName?: string
}

export function Modal({ open, title, onClose, children, widthClassName }: ModalProps) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    if (open) window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      {/* Dialog */}
      <div className={`relative z-10 max-h-[85vh] ${widthClassName || 'w-[720px]'} rounded border border-neutral-700 bg-[var(--panel-2)] shadow-xl overflow-hidden`}> 
        <div className="flex items-center justify-between gap-2 border-b border-neutral-700 px-4 py-2">
          <h2 className="text-sm font-semibold">{title}</h2>
          <button className="text-[var(--text-dim)] hover:text-white" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="p-4 overflow-auto">
          {children}
        </div>
      </div>
    </div>
  )
}
