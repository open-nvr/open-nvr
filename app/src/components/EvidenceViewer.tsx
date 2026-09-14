/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// Every photo a record carries, full size.
//
// A thumbnail answers "is this worth my attention"; this answers "who
// was it" — which for a scanner-flagged person, or for the guard who
// skipped a surface, is the entire point of the record.
//
// It is shared rather than per-surface because the alert inbox and the
// screening ledger hold the SAME photographs, reached two different
// ways: an alert is raised only when something went wrong, while every
// screening is kept. A viewer that existed on one and not the other is
// how four captured photos came to be visible as one 32px thumbnail.
//
// The owner supplies `fetchBlob` because the two routes are scoped
// differently (`/alerts-inbox/{id}/images/{name}` vs
// `/guardscan/screenings/{id}/images/{name}`); everything else about
// showing a photograph is the same on both.

import type { ReactNode } from 'react'
import { X } from 'lucide-react'
import { AuthedImage } from './AuthedImage'

export type EvidenceViewerProps = {
  /** Identifies the record — the alarm title, or the screening's verdict. */
  title: string
  /** Time, camera, kind: whatever names this record in one dim line. */
  subtitle?: string
  /** Photo names on this record, e.g. ['face', 'body', 'scene', 'guard_face']. */
  images: string[]
  /** Stable prefix for the per-image react-query key. */
  queryKeyPrefix: (string | number)[]
  fetchBlob: (name: string, signal?: AbortSignal) => Promise<{ data: any }>
  /**
   * Rendered under the photographs.
   *
   * "What happened here" should have ONE answer, not a photo overlay
   * and a video somewhere else — so the footage of a record goes in the
   * same place its stills do. Callers with nothing to add pass nothing
   * and get exactly what they had.
   */
  extra?: ReactNode
  onClose: () => void
}

/** 'guard_face' -> 'Guard face'. The names are wire keys, not labels. */
function caption(name: string): string {
  const words = name.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

export function EvidenceViewer({
  title, subtitle, images, queryKeyPrefix, fetchBlob, extra, onClose,
}: EvidenceViewerProps) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`Evidence for: ${title}`}
      // Escape and a click on the backdrop both close it: a photo
      // overlay that traps the operator is worse than no overlay.
      onClick={onClose}
      onKeyDown={(e) => { if (e.key === 'Escape') onClose() }}
      tabIndex={-1}
      ref={(el) => el?.focus()}
    >
      <div
        className="max-h-full w-full max-w-3xl overflow-auto rounded-lg border
                   border-[var(--border)] bg-[var(--panel-2)] p-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="truncate font-semibold">{title}</div>
            {subtitle && (
              <div className="text-xs text-[var(--text-dim)]">{subtitle}</div>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded p-1 text-[var(--text-dim)] hover:bg-[var(--panel)]
                       hover:text-[var(--text)]"
          >
            <X size={16} />
          </button>
        </div>
        <div className="flex flex-wrap gap-3">
          {images.map((name) => (
            <figure key={name} className="m-0">
              <AuthedImage
                queryKey={[...queryKeyPrefix, name]}
                fetchBlob={(signal) => fetchBlob(name, signal)}
                alt={`${caption(name)} for: ${title}`}
                className="max-h-[60vh] rounded border border-[var(--border)]"
              />
              <figcaption className="mt-1 text-center text-[11px]
                                     text-[var(--text-dim)]">
                {caption(name)}
              </figcaption>
            </figure>
          ))}
        </div>
        {extra}
      </div>
    </div>
  )
}
