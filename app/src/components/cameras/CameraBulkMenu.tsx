// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { clsx } from 'clsx'
import { useClickOutside } from '../../hooks/useClickOutside'
import { useTranslation } from '../../i18n'

export type BulkAction =
  | 'location'
  | 'detection-on' | 'detection-off'
  | 'activate' | 'deactivate'
  | 'pause' | 'resume'
  | 'assign' | 'delete'

/**
 * "Actions" for the cameras ticked in the list: every bulk operation in one
 * menu. Each group shows only to someone allowed to do it: camera managers
 * edit, assign and delete, pausing needs recordings.pause, resuming either.
 */
export function CameraBulkMenu({
  canManage, canPause, canResume, disabled, onPick,
}: {
  canManage: boolean
  canPause: boolean
  canResume: boolean
  disabled?: boolean
  onPick: (action: BulkAction) => void
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useClickOutside(ref, open, () => setOpen(false))

  type Item = [BulkAction, string]
  const manage: Item[][] = canManage ? [
    [['location', t('cameraBulk.setLocation')]],
    [['detection-on', t('cameraBulk.detectionOn')], ['detection-off', t('cameraBulk.detectionOff')]],
    [['activate', t('cameraBulk.activate')], ['deactivate', t('cameraBulk.deactivate')]],
  ] : []
  const recording: Item[] = [
    ...(canPause ? [['pause', t('recordingPause.pause')] as Item] : []),
    ...(canResume ? [['resume', t('recordingPause.resume')] as Item] : []),
  ]
  const groups = [
    ...manage,
    ...(recording.length ? [recording] : []),
    ...(canManage ? [[['assign', t('cameraBulk.assignPermissions')]] as Item[]] : []),
    ...(canManage ? [[['delete', t('cameraBulk.delete')]] as Item[]] : []),
  ]
  if (groups.length === 0) return null

  return (
    <div className="relative" ref={ref} onKeyDown={(e) => { if (e.key === 'Escape' && open) { e.stopPropagation(); setOpen(false) } }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        data-testid="camera-bulk-actions"
        className={clsx(
          'h-7 px-2.5 inline-flex items-center gap-1 text-[13px] border disabled:opacity-50',
          open
            ? 'border-[var(--accent)] text-[var(--accent)]'
            : 'border-[var(--border)] text-[var(--text)] hover:bg-[var(--panel-2)]',
        )}
      >
        {t('cameraBulk.actions')}
        <ChevronDown size={13} aria-hidden="true" />
      </button>
      {open && (
        <div role="menu" className="absolute right-0 top-full mt-1 z-50 w-56 bg-[var(--panel)] border border-[var(--border)] shadow-lg py-0.5">
          {groups.map((group, i) => (
            <div key={i} className={i > 0 ? 'border-t border-[var(--border)] mt-0.5 pt-0.5' : undefined}>
              {group.map(([action, label]) => (
                <button
                  key={action}
                  type="button"
                  role="menuitem"
                  data-testid={`camera-bulk-${action}`}
                  className={clsx(
                    'w-full text-left px-3 py-1.5 text-[13px] hover:bg-[var(--panel-2)] focus-visible:bg-[var(--panel-2)] focus:outline-none',
                    action === 'delete' && 'text-[var(--danger)]',
                  )}
                  onClick={() => { setOpen(false); onPick(action) }}
                >
                  {label}
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
