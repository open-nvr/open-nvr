// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The one way to ask "are you sure?".
 *
 *     const confirm = useConfirm()
 *     if (!(await confirm({ title: 'Delete camera?', danger: true, confirmLabel: 'Delete' }))) return
 *
 * Replaces `window.confirm`, which could not be themed, translated, or made to
 * explain consequences. It is ASYNC where `window.confirm` was not, so when
 * converting a call site the code after it must stay behind the `await` --
 * nothing may run before the operator has answered.
 *
 * Destructive confirmations (`danger`) focus Cancel, not Confirm: a stray
 * Enter on a dialog that deletes something should do nothing.
 * `requireText` asks the operator to type a word (a camera name, "DELETE")
 * before Confirm enables, for actions that cannot be undone.
 */

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import { Modal } from '../Modal'
import { Button } from './index'
import { useTranslation } from '../../i18n'

export type ConfirmOptions = {
  title: ReactNode
  /** What will happen, in plain words. */
  message?: ReactNode
  /** Verb for the confirming button ("Delete", "Block"). Defaults to "Confirm". */
  confirmLabel?: string
  cancelLabel?: string
  /** Red confirm button and a warning icon; focuses Cancel by default. */
  danger?: boolean
  /** Text the operator must type exactly before Confirm is enabled. */
  requireText?: string
}

type Pending = ConfirmOptions & { resolve: (ok: boolean) => void }

const ConfirmContext = createContext<((options: ConfirmOptions) => Promise<boolean>) | null>(null)

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null)

  const confirm = useCallback(
    (options: ConfirmOptions) =>
      new Promise<boolean>((resolve) => {
        setPending((current) => {
          // A second request while one is open answers the first "no":
          // two stacked "are you sure?" dialogs would be ambiguous.
          current?.resolve(false)
          return { ...options, resolve }
        })
      }),
    [],
  )

  const answer = useCallback((ok: boolean) => {
    setPending((current) => {
      current?.resolve(ok)
      return null
    })
  }, [])

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {pending && <ConfirmDialog pending={pending} onAnswer={answer} />}
    </ConfirmContext.Provider>
  )
}

function ConfirmDialog({ pending, onAnswer }: { pending: Pending; onAnswer: (ok: boolean) => void }) {
  const { t } = useTranslation()
  const [typed, setTyped] = useState('')
  const cancelRef = useRef<HTMLButtonElement>(null)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const needsText = Boolean(pending.requireText)
  const canConfirm = !needsText || typed === pending.requireText

  useEffect(() => {
    if (needsText) return // the text input takes focus instead
    ;(pending.danger ? cancelRef : confirmRef).current?.focus()
  }, [pending, needsText])

  return (
    <Modal
      open
      onClose={() => onAnswer(false)}
      widthClassName="w-[440px] max-w-[92vw]"
      data-testid="confirm-dialog"
      title={
        <>
          {pending.danger && <AlertTriangle size={16} className="text-[var(--danger)]" />}
          {pending.title}
        </>
      }
      footer={
        <div className="flex justify-end gap-2">
          <Button ref={cancelRef} variant="outline" size="sm" onClick={() => onAnswer(false)}>
            {pending.cancelLabel ?? t('common.cancel')}
          </Button>
          <Button
            ref={confirmRef}
            variant={pending.danger ? 'danger' : 'primary'}
            size="sm"
            disabled={!canConfirm}
            onClick={() => onAnswer(true)}
          >
            {pending.confirmLabel ?? t('common.confirm')}
          </Button>
        </div>
      }
    >
      {pending.message && <div className="text-sm text-[var(--text)] leading-relaxed">{pending.message}</div>}
      {needsText && (
        <label className="mt-3 flex flex-col gap-1 text-xs text-[var(--text-dim)]">
          {t('confirm.typeToConfirm', { text: pending.requireText ?? '' })}
          <input
            autoFocus
            className="input font-mono"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && canConfirm) onAnswer(true) }}
          />
        </label>
      )}
    </Modal>
  )
}

export function useConfirm() {
  const confirm = useContext(ConfirmContext)
  if (!confirm) throw new Error('useConfirm must be used inside ConfirmProvider')
  return confirm
}
