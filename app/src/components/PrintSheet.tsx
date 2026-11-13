/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// A report as a sheet of paper, in an overlay, that prints.
//
// One click gives a facility manager or a society committee a clean
// document to file. "Save as PDF" is the browser's own print dialog —
// zero dependencies, works everywhere, and there is no server-side
// renderer to maintain (this repo has no PDF library at all, on
// purpose).
//
// The shell is shared because the two reports that already do this —
// Occupancy and Vehicles — carry a byte-identical copy of the print
// CSS, the scrim, the sheet and the toolbar between them. This is the
// third, which is where a duplicated block turns into a component.
// Those two still hold their own copies; they can move over when they
// are next touched, since rewriting two working pages is not a job to
// take on in passing.
//
// The print rule is `visibility`, not `display`: hiding the app with
// `display:none` collapses the layout the sheet is positioned against,
// and the sheet prints in the wrong place.

import type { ReactNode } from 'react'
import { FileText } from 'lucide-react'
import { Button } from './ui'

export type PrintSheetProps = {
  /** Rendered in the on-screen toolbar, left of the Print button —
   *  a period picker, usually. Never printed. */
  controls?: ReactNode
  /** Whether there is anything worth printing yet. */
  ready?: boolean
  onClose: () => void
  children: ReactNode
}

export function PrintSheet({ controls, ready = true, onClose, children }: PrintSheetProps) {
  return (
    <div
      className="fixed inset-0 z-50 overflow-y-auto bg-black/50 print:overflow-visible"
      role="dialog"
      aria-modal="true"
      onKeyDown={(e) => { if (e.key === 'Escape') onClose() }}
      tabIndex={-1}
      ref={(el) => el?.focus()}
    >
      <style>{`
        @media print {
          body * { visibility: hidden !important; }
          .print-report, .print-report * { visibility: visible !important; }
          .print-report { position: absolute !important; inset: 0 !important;
            margin: 0 !important; box-shadow: none !important;
            border-radius: 0 !important; }
          .no-print { display: none !important; }
        }
      `}</style>
      {/* White paper, dark ink — a printed report is not themed, and a
          dark-mode sheet would come out of the printer as a black page. */}
      <div className="print-report mx-auto my-6 max-w-3xl rounded-lg bg-white p-8
                      text-neutral-900 shadow-xl print:p-0">
        <div className="no-print mb-6 flex items-center gap-2 border-b border-neutral-200 pb-4">
          {controls}
          <Button onClick={() => window.print()} disabled={!ready}>
            <FileText size={14} /> Print / Save as PDF
          </Button>
          <button
            type="button"
            className="ml-auto text-sm text-neutral-500 hover:text-neutral-900"
            onClick={onClose}
          >
            ✕ Close
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}
