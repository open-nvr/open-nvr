import React, { createContext, useCallback, useContext, useState } from 'react'
import { messages } from '../messages'

type ConfirmOptions = {
  title?: string
  description?: string
  confirmText?: string
  cancelText?: string
}

type ConfirmContextType = {
  confirm: (opts: ConfirmOptions) => Promise<boolean>
}

const ConfirmContext = createContext<ConfirmContextType | undefined>(undefined)

export function useConfirm(): ConfirmContextType {
  const ctx = useContext(ConfirmContext)
  if (!ctx) throw new Error('useConfirm must be used within ConfirmProvider')
  return ctx
}

export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<{
    open: boolean
    opts: ConfirmOptions
    resolve?: (v: boolean) => void
  }>({ open: false, opts: {} })

  const confirm = useCallback((opts: ConfirmOptions) => {
    return new Promise<boolean>((resolve) => {
      setState({ open: true, opts, resolve })
    })
  }, [])

  const onClose = (value: boolean) => {
    const r = state.resolve
    setState({ open: false, opts: {} })
    if (r) r(value)
  }

  const { open, opts } = state
  const title = opts.title || messages.common.confirm
  const cancelText = opts.cancelText || messages.common.cancel
  const confirmText = opts.confirmText || messages.common.delete

  return (
    <ConfirmContext.Provider value={{ confirm }}>
      {children}
      {open && (
        <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/40">
          <div className="w-full max-w-sm rounded-lg bg-white border shadow-lg p-4">
            <div className="text-base font-semibold text-gray-900 mb-1">{title}</div>
            {opts.description && (
              <div className="text-sm text-gray-600 mb-3 whitespace-pre-line">{opts.description}</div>
            )}
            <div className="flex justify-end gap-2">
              <button className="px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200" onClick={() => onClose(false)}>{cancelText}</button>
              <button className="px-3 py-1.5 rounded-md bg-red-600 text-white hover:bg-red-700" onClick={() => onClose(true)}>{confirmText}</button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  )
}


