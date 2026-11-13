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

import { createContext, useContext, useState, useCallback, ReactNode } from 'react'
import { X, AlertCircle, CheckCircle, Info, AlertTriangle } from 'lucide-react'

type SnackbarType = 'error' | 'success' | 'info' | 'warning'

interface SnackbarMessage {
  id: number
  message: string
  type: SnackbarType
}

interface SnackbarContextType {
  showSnackbar: (message: string, type?: SnackbarType) => void
  showError: (message: string) => void
  showSuccess: (message: string) => void
  showInfo: (message: string) => void
  showWarning: (message: string) => void
}

const SnackbarContext = createContext<SnackbarContextType | null>(null)

export function useSnackbar() {
  const context = useContext(SnackbarContext)
  if (!context) {
    throw new Error('useSnackbar must be used within a SnackbarProvider')
  }
  return context
}

let snackbarId = 0

export function SnackbarProvider({ children }: { children: ReactNode }) {
  const [snackbars, setSnackbars] = useState<SnackbarMessage[]>([])

  const removeSnackbar = useCallback((id: number) => {
    setSnackbars(prev => prev.filter(s => s.id !== id))
  }, [])

  const showSnackbar = useCallback((message: string, type: SnackbarType = 'info') => {
    const id = ++snackbarId
    setSnackbars(prev => [...prev, { id, message, type }])
    
    // Auto-remove after 5 seconds
    setTimeout(() => {
      removeSnackbar(id)
    }, 5000)
  }, [removeSnackbar])

  const showError = useCallback((message: string) => showSnackbar(message, 'error'), [showSnackbar])
  const showSuccess = useCallback((message: string) => showSnackbar(message, 'success'), [showSnackbar])
  const showInfo = useCallback((message: string) => showSnackbar(message, 'info'), [showSnackbar])
  const showWarning = useCallback((message: string) => showSnackbar(message, 'warning'), [showSnackbar])

  return (
    <SnackbarContext.Provider value={{ showSnackbar, showError, showSuccess, showInfo, showWarning }}>
      {children}
      <SnackbarContainer snackbars={snackbars} onClose={removeSnackbar} />
    </SnackbarContext.Provider>
  )
}

function SnackbarContainer({ snackbars, onClose }: { snackbars: SnackbarMessage[], onClose: (id: number) => void }) {
  if (snackbars.length === 0) return null

  return (
    <div className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2 max-w-md">
      {snackbars.map(snackbar => (
        <SnackbarItem key={snackbar.id} snackbar={snackbar} onClose={() => onClose(snackbar.id)} />
      ))}
    </div>
  )
}

function SnackbarItem({ snackbar, onClose }: { snackbar: SnackbarMessage, onClose: () => void }) {
  const { type, message } = snackbar

  // Theme tokens, so a toast reads correctly in the light theme too: a panel
  // surface with the status carried by an inked edge and icon, not by a
  // dark tinted background that turned into a smudge on white.
  const styles: Record<SnackbarType, { ink: string, icon: ReactNode }> = {
    error: { ink: 'var(--danger)', icon: <AlertCircle size={18} className="flex-shrink-0" /> },
    success: { ink: 'var(--ok)', icon: <CheckCircle size={18} className="flex-shrink-0" /> },
    info: { ink: 'var(--accent)', icon: <Info size={18} className="flex-shrink-0" /> },
    warning: { ink: 'var(--warn)', icon: <AlertTriangle size={18} className="flex-shrink-0" /> },
  }

  const style = styles[type]

  return (
    <div
      className="bg-[var(--panel)] text-[var(--text)] border border-[var(--border)] shadow-lg p-3 pr-10 relative animate-slide-in-right min-w-[280px]"
      style={{ boxShadow: `inset 3px 0 0 ${style.ink}, 0 10px 15px -3px rgb(0 0 0 / 0.25)` }}
      role="alert"
    >
      <div className="flex items-start gap-2">
        <span className="flex" style={{ color: style.ink }}>{style.icon}</span>
        <p className="text-sm">{message}</p>
      </div>
      <button
        onClick={onClose}
        className="absolute top-2 right-2 p-1 text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel-2)] transition-colors"
        aria-label="Close"
      >
        <X size={14} />
      </button>
    </div>
  )
}
