import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'

type ToastKind = 'success' | 'error' | 'info'
type ToastItem = { id: number; kind: ToastKind; message: string }

type ToastContextType = {
  showToast: (kind: ToastKind, message: string, durationMs?: number) => void
}

const ToastContext = createContext<ToastContextType | undefined>(undefined)

export function useToast(): ToastContextType {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within ToastProvider')
  return ctx
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const idRef = useRef(1)
  const timersRef = useRef<Record<number, number>>({})

  const remove = useCallback((id: number) => {
    setToasts(prev => prev.filter(t => t.id !== id))
    const tid = timersRef.current[id]
    if (tid) {
      window.clearTimeout(tid)
      delete timersRef.current[id]
    }
  }, [])

  const showToast = useCallback((kind: ToastKind, message: string, durationMs = 2500) => {
    const id = idRef.current++
    setToasts(prev => [...prev, { id, kind, message }])
    timersRef.current[id] = window.setTimeout(() => remove(id), durationMs)
  }, [remove])

  useEffect(() => () => {
    Object.values(timersRef.current).forEach(t => window.clearTimeout(t))
    timersRef.current = {}
  }, [])

  return (
    <ToastContext.Provider value={{ showToast }}>
      {children}
      <div className="fixed right-4 top-4 z-[100] space-y-2">
        {toasts.map(t => (
          <div key={t.id} className={`px-4 py-2 rounded-md shadow border text-sm ${t.kind === 'success' ? 'bg-green-50 text-green-800 border-green-200' : t.kind === 'error' ? 'bg-red-50 text-red-800 border-red-200' : 'bg-gray-800 text-white border-gray-700'}`}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}


