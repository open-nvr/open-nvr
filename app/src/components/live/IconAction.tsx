// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { clsx } from 'clsx'

/** A small icon button for tour cards and editor rows; `danger` for removals. */
export function IconAction({ label, onClick, disabled, danger, children }: {
  label: string
  onClick: () => void
  disabled?: boolean
  danger?: boolean
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={clsx('p-1.5 border border-transparent disabled:opacity-40 disabled:cursor-not-allowed',
        danger ? 'text-[var(--danger)] hover:bg-[color-mix(in_oklab,var(--danger)_14%,transparent)]' : 'text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel)]')}
    >
      {children}
    </button>
  )
}
