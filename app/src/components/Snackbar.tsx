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

  const styles: Record<SnackbarType, { bg: string, border: string, text: string, icon: ReactNode }> = {
    error: {
      bg: 'bg-red-900/90',
      border: 'border-red-700',
      text: 'text-red-100',
      icon: <AlertCircle size={18} className="text-red-400 flex-shrink-0" />
    },
    success: {
      bg: 'bg-green-900/90',
      border: 'border-green-700',
      text: 'text-green-100',
      icon: <CheckCircle size={18} className="text-green-400 flex-shrink-0" />
    },
    info: {
      bg: 'bg-blue-900/90',
      border: 'border-blue-700',
      text: 'text-blue-100',
      icon: <Info size={18} className="text-blue-400 flex-shrink-0" />
    },
    warning: {
      bg: 'bg-yellow-900/90',
      border: 'border-yellow-700',
      text: 'text-yellow-100',
      icon: <AlertTriangle size={18} className="text-yellow-400 flex-shrink-0" />
    }
  }

  const style = styles[type]

  return (
    <div
      className={`${style.bg} ${style.border} ${style.text} border rounded-lg shadow-lg p-3 pr-10 relative animate-slide-in-right min-w-[280px]`}
      role="alert"
    >
      <div className="flex items-start gap-2">
        {style.icon}
        <p className="text-sm">{message}</p>
      </div>
      <button
        onClick={onClose}
        className="absolute top-2 right-2 p-1 hover:bg-white/10 rounded transition-colors"
        aria-label="Close"
      >
        <X size={14} />
      </button>
    </div>
  )
}
