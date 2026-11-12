// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * One row in a webcam list, the same for every kind: the browser's own
 * cameras, the USB webcams MediaMTX finds on a Linux server, and the ones
 * the host agent finds on Docker Desktop. A webcam that already feeds a
 * camera stays in the list, says which camera, and cannot be picked: one
 * webcam feeds one camera (a second would be the same picture recorded
 * twice, and on the server a capture that fails "device busy").
 *
 * One bordered list with dividers rather than a stack of boxes: an icon
 * tile, the name with a quiet second line under it, and a radio dot.
 */

import type { ReactNode } from 'react'
import { clsx } from 'clsx'

/** "HP HD Camera (30c9:0011)" → "HP HD Camera": the USB vendor:product
 *  ids a browser appends mean nothing to a person. */
export function webcamLabel(label: string): string {
  return label.replace(/\s*\([0-9a-f]{4}:[0-9a-f]{4}\)\s*$/i, '').trim()
}

export function WebcamList({ children }: { children: ReactNode }) {
  return (
    <div
      role="radiogroup"
      aria-label="Webcam"
      className="overflow-hidden rounded-[var(--radius)] border border-[var(--border)] divide-y divide-[var(--border)]"
    >
      {children}
    </div>
  )
}

export function WebcamOption({
  icon, name, subtitle, selected, takenBy, onSelect,
}: {
  icon: ReactNode
  name: string
  /** The quiet second line: where the webcam is ("On this device", "USB · video0"). */
  subtitle?: string
  selected: boolean
  /** The camera this webcam already feeds; the row is then not pickable. */
  takenBy?: string | null
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      disabled={!!takenBy}
      onClick={onSelect}
      className={clsx(
        'w-full flex items-center gap-3 px-3 py-2 text-left transition-colors disabled:cursor-not-allowed',
        selected ? 'bg-[var(--accent)]/10' : takenBy ? '' : 'hover:bg-[var(--bg-2)]',
      )}
    >
      <span
        className={clsx(
          'flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius)]',
          selected ? 'bg-[var(--accent)]/20 text-[var(--accent)]' : 'bg-[var(--bg-2)] text-[var(--text-dim)]',
          takenBy && 'opacity-50',
        )}
      >
        {icon}
      </span>
      <span className={clsx('flex-1 min-w-0', takenBy && 'opacity-60')}>
        <span className="block truncate text-[13px] font-medium leading-tight">{name}</span>
        <span className="block truncate text-[11px] leading-tight mt-0.5 text-[var(--text-dim)]">
          {takenBy ? `In use by “${takenBy}”` : subtitle}
        </span>
      </span>
      <span
        aria-hidden
        className={clsx(
          'flex h-4 w-4 shrink-0 items-center justify-center rounded-full border',
          selected ? 'border-[var(--accent)]' : 'border-[var(--border)]',
          takenBy && 'opacity-40',
        )}
      >
        {selected && <span className="h-2 w-2 rounded-full bg-[var(--accent)]" />}
      </span>
    </button>
  )
}
