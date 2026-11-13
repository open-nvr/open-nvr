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

import { CheckCircle2, Download, Loader2, X, XCircle } from 'lucide-react'
import { useTranslation } from '../i18n'

export type ClipJobPhase = 'starting' | 'preparing' | 'downloading' | 'done' | 'browser' | 'failed' | 'cancelled'

export interface ClipJob {
  id: string
  camera: string
  range: string
  filename: string
  durationSec: number
  phase: ClipJobPhase
  bytes: number
  mediaSeconds: number
  error?: string
}

export const clipJobRunning = (j: ClipJob) => j.phase === 'starting' || j.phase === 'preparing' || j.phase === 'downloading'

function formatBytes(n: number) {
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(2)} GB`
}

/**
 * Clip downloads in flight and just finished, pinned bottom-right so they
 * stay in view while the operator keeps working. Progress is media time
 * reached over clip length (see lib/clipDownload.ts).
 */
export function ClipDownloadPanel({
  jobs,
  onCancel,
  onDismiss,
  onClearFinished,
}: {
  jobs: ClipJob[]
  onCancel: (id: string) => void
  onDismiss: (id: string) => void
  onClearFinished: () => void
}) {
  const { t } = useTranslation()
  if (jobs.length === 0) return null
  const running = jobs.filter(clipJobRunning).length

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-4 right-4 z-50 w-[22rem] max-w-[calc(100vw-2rem)] border border-[var(--border)] bg-[var(--panel-2)] shadow-2xl"
    >
      <div className="h-8 flex items-center gap-2 px-2.5 border-b border-[var(--border)] bg-[color-mix(in_oklab,var(--bg-2)_55%,var(--panel-2))]">
        <Download size={13} className="text-[var(--accent)]" />
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em]">{t('playback.dlTitle')}</span>
        {running > 0 && (
          <span className="text-[11px] font-mono text-[var(--text-dim)]">{t('playback.dlRunning', { n: running })}</span>
        )}
        {running < jobs.length && (
          <button onClick={onClearFinished} className="ml-auto text-[11px] text-[var(--text-dim)] hover:text-[var(--text)]">
            {t('playback.dlClear')}
          </button>
        )}
      </div>
      <ul className="max-h-72 overflow-y-auto divide-y divide-[var(--border)]">
        {jobs.map((j) => {
          const live = clipJobRunning(j)
          const pct = j.durationSec > 0 ? Math.min(99, Math.round((j.mediaSeconds / j.durationSec) * 100)) : 0
          const status =
            j.phase === 'starting'
              ? t('playback.dlStarting')
              : j.phase === 'preparing'
                ? t('playback.dlPreparing')
                : j.phase === 'downloading'
                  ? t('playback.dlDownloading', { pct: `${pct}%`, size: formatBytes(j.bytes) })
                  : j.phase === 'done'
                    ? t('playback.dlSaved', { size: formatBytes(j.bytes) })
                    : j.phase === 'browser'
                      ? t('playback.dlBrowser')
                      : j.phase === 'cancelled'
                        ? t('playback.dlCancelled')
                        : `${t('playback.dlFailed')}: ${j.error || ''}`
          return (
            <li key={j.id} className="px-2.5 py-2">
              <div className="flex items-center gap-2 min-w-0">
                {j.phase === 'done' || j.phase === 'browser' ? (
                  <CheckCircle2 size={14} className="text-[var(--ok)] shrink-0" />
                ) : j.phase === 'failed' || j.phase === 'cancelled' ? (
                  <XCircle size={14} className="text-[var(--danger,#ef4444)] shrink-0" />
                ) : (
                  <Loader2 size={14} className="animate-spin text-[var(--accent)] shrink-0" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="text-xs truncate" title={j.filename}>
                    {j.camera} <span className="text-[var(--text-dim)] font-mono">· {j.range}</span>
                  </div>
                  <div
                    className={`text-[11px] truncate ${j.phase === 'failed' ? 'text-[var(--danger,#ef4444)]' : 'text-[var(--text-dim)]'}`}
                    title={status}
                  >
                    {status}
                  </div>
                </div>
                <button
                  onClick={() => (live ? onCancel(j.id) : onDismiss(j.id))}
                  title={live ? t('common.cancel') : t('playback.dlDismiss')}
                  aria-label={live ? t('common.cancel') : t('playback.dlDismiss')}
                  className="shrink-0 p-1 text-[var(--text-dim)] hover:text-[var(--text)]"
                >
                  <X size={13} />
                </button>
              </div>
              {live && (
                <div className="mt-1.5 h-1.5 bg-[var(--panel)] overflow-hidden" aria-hidden="true">
                  {j.phase === 'downloading' && j.mediaSeconds > 0 ? (
                    <div className="h-full bg-[var(--accent)] transition-[width] duration-300" style={{ width: `${pct}%` }} />
                  ) : (
                    <div className="h-full w-1/3 bg-[var(--accent)]/70 animate-pulse" />
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
