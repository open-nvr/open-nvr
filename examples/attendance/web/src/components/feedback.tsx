// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Toasts, and a confirm dialog that is a promise (attendly's
// ToastProvider and ConfirmProvider, one file).

import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'
import { CheckCircle2, Info, XCircle } from 'lucide-react'
import { Button, Modal } from './ui'

type ToastKind = 'success' | 'error' | 'info'
type Toast = { id: number; kind: ToastKind; text: string }
type ConfirmAsk = { title: string; body: ReactNode; confirm?: string; danger?: boolean }

type Feedback = {
  toast: (kind: ToastKind, text: string) => void
  confirm: (ask: ConfirmAsk) => Promise<boolean>
}

const Ctx = createContext<Feedback | null>(null)

export function FeedbackProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const [ask, setAsk] = useState<ConfirmAsk | null>(null)
  const answer = useRef<(ok: boolean) => void>(() => {})
  const next = useRef(0)

  const toast = useCallback((kind: ToastKind, text: string) => {
    const id = ++next.current
    setToasts((t) => [...t, { id, kind, text }])
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === 'error' ? 7000 : 4000)
  }, [])

  const confirm = useCallback(
    (a: ConfirmAsk) =>
      new Promise<boolean>((resolve) => {
        answer.current = resolve
        setAsk(a)
      }),
    [],
  )

  const close = (ok: boolean) => {
    setAsk(null)
    answer.current(ok)
  }

  const icon = { success: CheckCircle2, error: XCircle, info: Info }
  const tone = {
    success: 'border-green-200 bg-green-50 text-green-800',
    error: 'border-red-200 bg-red-50 text-red-800',
    info: 'border-indigo-200 bg-indigo-50 text-indigo-800',
  }

  return (
    <Ctx.Provider value={{ toast, confirm }}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2" aria-live="polite">
        {toasts.map((t) => {
          const Icon = icon[t.kind]
          return (
            <div key={t.id} className={`pointer-events-auto flex items-start gap-2 rounded-lg border p-3 text-sm shadow-md ${tone[t.kind]}`}>
              <Icon className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{t.text}</span>
            </div>
          )
        })}
      </div>
      {ask && (
        <Modal
          title={ask.title}
          onClose={() => close(false)}
          footer={
            <>
              <Button variant="secondary" onClick={() => close(false)}>
                Cancel
              </Button>
              <Button variant={ask.danger ? 'danger' : 'primary'} onClick={() => close(true)}>
                {ask.confirm ?? 'Continue'}
              </Button>
            </>
          }
        >
          <div className="text-sm text-gray-700">{ask.body}</div>
        </Modal>
      )}
    </Ctx.Provider>
  )
}

export function useFeedback(): Feedback {
  const v = useContext(Ctx)
  if (!v) throw new Error('useFeedback outside FeedbackProvider')
  return v
}
