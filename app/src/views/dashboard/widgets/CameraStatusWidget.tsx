// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { Camera } from 'lucide-react'
import { useTranslation } from '../../../i18n'
import { useCameras, useRecordingsByDate } from '../../../lib/queries'
import { formatDuration } from '../../../lib/time'
import { Skeleton } from '../../../components/ui'
import { WidgetFrame, cameraState, shortAge, STATE_INK, type CamState } from '../WidgetFrame'

const STATE_RANK: Record<CamState, number> = { error: 0, offline: 1, degraded: 2, online: 3 }
const capitalise = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

/**
 * Every camera on one dense list — stream state, recording state, how much
 * footage is on disk. Problems sort to the top, so a healthy fleet reads as
 * a column of green and a broken camera is the first row.
 */
export function CameraStatusWidget({ editing, onRemove }: { editing?: boolean; onRemove?: () => void }) {
  const { t } = useTranslation()
  const cams = useCameras()
  const recs = useRecordingsByDate()

  const footage = useMemo(() => {
    const m = new Map<number, number>()
    for (const c of recs.data?.cameras ?? []) {
      if (c.camera_id == null) continue
      m.set(c.camera_id, c.total_duration ?? (c.recordings ?? []).reduce((s, r) => s + (r.total_duration || 0), 0))
    }
    return m
  }, [recs.data])

  const rows = useMemo(() => {
    const list = [...(cams.data?.cameras ?? [])]
    const recRank = (s?: string | null) => (s === 'stalled' || s === 'degraded' ? 0 : s === 'recording' ? 2 : 1)
    list.sort((a, b) =>
      STATE_RANK[cameraState(a)] - STATE_RANK[cameraState(b)] ||
      recRank(a.recording_state) - recRank(b.recording_state) ||
      a.name.localeCompare(b.name))
    return list
  }, [cams.data])

  const stateLabel: Record<CamState, string> = {
    online: t('dashboard.statusOnline'),
    degraded: t('dashboard.statusDegraded'),
    offline: t('dashboard.statusOffline'),
    error: t('dashboard.statusError'),
  }
  const recLabel = (s?: string | null) =>
    s === 'recording' ? t('dashboard.recShort')
      : s === 'stalled' ? capitalise(t('dashboard.stalled'))
        : s === 'degraded' ? t('dashboard.statusDegraded')
          : s === 'never' ? t('dashboard.recNever')
            : t('dashboard.recOff')
  const recInk = (s?: string | null) =>
    s === 'recording' ? 'var(--ok)'
      : s === 'stalled' ? 'var(--warn)'
        : s === 'degraded' ? 'var(--danger)'
          : 'var(--text-dim)'

  return (
    <WidgetFrame
      icon={<Camera size={13} />}
      title={t('dashboard.w.cameras')}
      editing={editing}
      onRemove={onRemove}
      actions={<Link to="/cameras" className="text-[11px] text-[var(--accent)] hover:underline px-1">{t('dashboard.manage')}</Link>}
    >
      {cams.isPending ? (
        <div className="p-2 space-y-1">{Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-6" />)}</div>
      ) : rows.length === 0 ? (
        <div className="p-3 text-xs text-[var(--text-dim)]">{t('dashboard.noCameras')}</div>
      ) : (
        <div className="absolute inset-0 overflow-auto thin-scroll">
          <table className="w-full text-[11.5px]">
            <thead className="sticky top-0 bg-[var(--panel-2)] z-[1]">
              <tr className="text-left text-[10px] uppercase tracking-wider text-[var(--text-dim)]">
                <th scope="col" className="font-medium px-2.5 py-1">{t('dashboard.camera')}</th>
                <th scope="col" className="font-medium px-2 py-1">{t('dashboard.stream')}</th>
                <th scope="col" className="font-medium px-2 py-1">{t('dashboard.recording')}</th>
                <th scope="col" className="font-medium px-2.5 py-1 text-right">{t('dashboard.footage')}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border)]">
              {rows.map((c) => {
                const st = cameraState(c)
                const secs = footage.get(c.id)
                return (
                  <tr key={c.id} className="hover:bg-[var(--panel)]">
                    <td className="px-2.5 py-1 max-w-0 w-[45%]">
                      <div className="truncate text-[var(--text)]" title={`${c.name} · ${c.ip_address}`}>{c.name || `Camera ${c.id}`}</div>
                    </td>
                    <td className="px-2 py-1 whitespace-nowrap">
                      <span className="inline-flex items-center gap-1.5">
                        <span className="w-1.5 h-1.5 rounded-full" style={{ background: STATE_INK[st] }} aria-hidden="true" />
                        <span style={{ color: st === 'online' ? 'var(--text)' : STATE_INK[st] }}>{stateLabel[st]}</span>
                      </span>
                    </td>
                    <td className="px-2 py-1 whitespace-nowrap" title={c.last_recording_at ? `${t('dashboard.lastFootage')} ${shortAge(c.last_recording_at)}` : undefined}>
                      <span style={{ color: recInk(c.recording_state) }}>{recLabel(c.recording_state)}</span>
                    </td>
                    <td className="px-2.5 py-1 text-right font-mono tabular-nums text-[var(--text-dim)] whitespace-nowrap">
                      {secs ? formatDuration(secs) : '—'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </WidgetFrame>
  )
}
