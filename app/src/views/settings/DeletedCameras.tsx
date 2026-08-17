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

// The bin. Cameras deleted from the Cameras page land here: irreversibly
// soft-deleted (never editable or reactivatable), streams torn down, but
// their recordings stay playable until retention ages them out. A superuser
// can purge one permanently — DB rows + recordings on disk — behind a typed
// confirmation phrase and a current TOTP code.

import { useCallback, useEffect, useState } from 'react'
import { Trash2, Film, ChevronDown, ChevronRight, Loader2 } from 'lucide-react'
import { apiService } from '../../lib/apiService'
import { useAuth } from '../../auth/AuthContext'
import { useSnackbar } from '../../components/Snackbar'
import { Modal } from '../../components/Modal'
import { PlaybackConsole } from '../../components/PlaybackConsole'

type DeletedCamera = {
  id: number
  name: string
  ip_address: string
  location?: string | null
  owner_id: number
  deleted_at: string
  path: string
}

type DayEntry = { date: string; total_duration?: number; segment_count?: number }

export function DeletedCameras() {
  const { user } = useAuth()
  const { showError, showSuccess } = useSnackbar()
  const isSuperuser = !!user?.is_superuser

  const [loading, setLoading] = useState(true)
  const [cameras, setCameras] = useState<DeletedCamera[]>([])

  // Per-camera recorded-day listings, loaded on expand.
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const [daysByCamera, setDaysByCamera] = useState<Record<number, DayEntry[] | 'loading'>>({})

  // DVR console target (reuses the normal playback console; the backend
  // resolves binned cameras on the history endpoints).
  const [consoleTarget, setConsoleTarget] = useState<{ cameraId: number; cameraName: string; date: string } | null>(null)

  // Hard-delete dialog state
  const [purging, setPurging] = useState<DeletedCamera | null>(null)
  const [phrase, setPhrase] = useState('')
  const [mfaCode, setMfaCode] = useState('')
  const [purgeBusy, setPurgeBusy] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await apiService.getDeletedCameras()
      setCameras(data?.cameras || [])
    } catch (e: any) {
      showError(e?.message || 'Failed to load deleted cameras')
    } finally {
      setLoading(false)
    }
  }, [showError])

  useEffect(() => { refresh() }, [refresh])

  const toggleExpand = async (cam: DeletedCamera) => {
    const next = new Set(expanded)
    if (next.has(cam.id)) {
      next.delete(cam.id)
      setExpanded(next)
      return
    }
    next.add(cam.id)
    setExpanded(next)
    if (daysByCamera[cam.id]) return
    setDaysByCamera((m) => ({ ...m, [cam.id]: 'loading' }))
    try {
      const { data } = await apiService.getRecordingsByDate(cam.id, { deletedOnly: true })
      const entry = (data?.cameras || []).find((c: any) => c.camera_id === cam.id)
      setDaysByCamera((m) => ({ ...m, [cam.id]: entry?.recordings || [] }))
    } catch (e: any) {
      setDaysByCamera((m) => ({ ...m, [cam.id]: [] }))
      showError(e?.message || 'Failed to load recordings')
    }
  }

  const expectedPhrase = purging ? `hard delete ${purging.name} and it's recording` : ''
  const phraseOk = phrase === expectedPhrase

  const closePurgeDialog = () => {
    setPurging(null)
    setPhrase('')
    setMfaCode('')
  }

  const onPurge = async () => {
    if (!purging || !phraseOk || !mfaCode.trim()) return
    setPurgeBusy(true)
    try {
      const { data } = await apiService.hardDeleteCamera(purging.id, phrase, mfaCode.trim())
      showSuccess(
        `Camera "${data?.camera_name ?? purging.name}" permanently deleted` +
        (data?.files === 'deleted' ? ' (recordings purged)' : '')
      )
      closePurgeDialog()
      await refresh()
    } catch (e: any) {
      showError(e?.message || 'Permanent delete failed')
    } finally {
      setPurgeBusy(false)
    }
  }

  const fmtWhen = (iso: string) => {
    try { return new Date(iso).toLocaleString() } catch { return iso }
  }
  const fmtHours = (secs?: number) =>
    secs ? `${(secs / 3600).toFixed(1)} h` : ''

  return (
    <div className="space-y-4 text-sm">
      <div>
        <div className="font-medium text-[var(--text)] mb-1">Deleted Cameras</div>
        <div className="text-[var(--text-dim)]">
          Deleted cameras cannot be edited or reactivated. Their recordings stay
          viewable here until retention removes them{isSuperuser ? ', or until the camera is permanently deleted' : ''}.
        </div>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-[var(--text-dim)]"><Loader2 size={16} className="animate-spin" /> Loading…</div>
      ) : cameras.length === 0 ? (
        <div className="text-[var(--text-dim)]">The bin is empty.</div>
      ) : (
        <div className="border border-neutral-700">
          <table className="w-full">
            <thead className="bg-[var(--panel-2)] text-left">
              <tr>
                <th className="p-2 w-8"></th>
                <th className="p-2">Name</th>
                <th className="p-2">IP Address</th>
                <th className="p-2">Location</th>
                <th className="p-2">Deleted</th>
                <th className="p-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {cameras.map((c) => (
                <>
                  <tr key={c.id} className="border-t border-neutral-800">
                    <td className="p-2 align-top">
                      <button
                        className="text-[var(--text-dim)] hover:text-[var(--text)]"
                        title="Show recordings"
                        onClick={() => toggleExpand(c)}
                      >
                        {expanded.has(c.id) ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                      </button>
                    </td>
                    <td className="p-2 align-top">{c.name}</td>
                    <td className="p-2 align-top">{c.ip_address}</td>
                    <td className="p-2 align-top">{c.location || '—'}</td>
                    <td className="p-2 align-top">{fmtWhen(c.deleted_at)}</td>
                    <td className="p-2 align-top text-right">
                      <div className="inline-flex items-center gap-2">
                        <button
                          className="px-2 py-1 border border-neutral-700 hover:bg-[var(--panel-2)] inline-flex items-center gap-1"
                          onClick={() => toggleExpand(c)}
                        >
                          <Film size={14} /> Recordings
                        </button>
                        {isSuperuser && (
                          <button
                            className="px-2 py-1 border border-red-800 text-red-400 hover:bg-red-950/40 inline-flex items-center gap-1"
                            onClick={() => setPurging(c)}
                          >
                            <Trash2 size={14} /> Delete Permanently
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                  {expanded.has(c.id) && (
                    <tr key={`${c.id}-days`} className="border-t border-neutral-800 bg-[var(--panel-2)]/50">
                      <td></td>
                      <td colSpan={5} className="p-2">
                        {daysByCamera[c.id] === 'loading' ? (
                          <div className="flex items-center gap-2 text-[var(--text-dim)]"><Loader2 size={14} className="animate-spin" /> Loading recordings…</div>
                        ) : !daysByCamera[c.id] || (daysByCamera[c.id] as DayEntry[]).length === 0 ? (
                          <div className="text-[var(--text-dim)]">No recordings left for this camera.</div>
                        ) : (
                          <div className="flex flex-wrap gap-2">
                            {(daysByCamera[c.id] as DayEntry[]).map((d) => (
                              <button
                                key={d.date}
                                className="px-2 py-1 border border-neutral-700 hover:bg-[var(--panel)] inline-flex items-center gap-1"
                                title={`Play recordings of ${d.date}`}
                                onClick={() => setConsoleTarget({ cameraId: c.id, cameraName: c.name, date: d.date })}
                              >
                                <Film size={13} /> {d.date}
                                {d.total_duration ? <span className="text-[var(--text-dim)]">· {fmtHours(d.total_duration)}</span> : null}
                              </button>
                            ))}
                          </div>
                        )}
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* DVR playback console for a binned camera's day */}
      {consoleTarget && (
        <PlaybackConsole
          cameraId={consoleTarget.cameraId}
          cameraName={consoleTarget.cameraName}
          date={consoleTarget.date}
          onClose={() => setConsoleTarget(null)}
        />
      )}

      {/* Hard-delete confirmation */}
      <Modal
        open={!!purging}
        title="Permanently delete camera"
        onClose={() => { if (!purgeBusy) closePurgeDialog() }}
      >
        {purging && (
          <div className="space-y-3 text-sm">
            <div>
              This removes <span className="font-medium">{purging.name}</span> and{' '}
              <span className="font-medium">all of its recordings</span> from the
              database and from disk. This cannot be undone.
            </div>
            <div>
              <div className="mb-1 text-[var(--text-dim)]">
                Type <span className="font-mono text-[var(--text)]">{expectedPhrase}</span> to confirm:
              </div>
              <input
                className="w-full bg-[var(--panel-2)] border border-neutral-700 px-2 py-1 font-mono"
                value={phrase}
                onChange={(e) => setPhrase(e.target.value)}
                placeholder={expectedPhrase}
                autoFocus
              />
            </div>
            <div>
              <div className="mb-1 text-[var(--text-dim)]">Current MFA code:</div>
              <input
                className="w-40 bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"
                value={mfaCode}
                onChange={(e) => setMfaCode(e.target.value)}
                placeholder="123456"
                inputMode="numeric"
                autoComplete="one-time-code"
              />
            </div>
            <div className="flex justify-end gap-2 pt-1">
              <button
                className="px-3 py-1 border border-neutral-700 hover:bg-[var(--panel-2)]"
                onClick={closePurgeDialog}
                disabled={purgeBusy}
              >
                Cancel
              </button>
              <button
                className="px-3 py-1 bg-red-700 text-white disabled:opacity-50"
                onClick={onPurge}
                disabled={purgeBusy || !phraseOk || !mfaCode.trim()}
              >
                {purgeBusy ? 'Deleting…' : 'Delete Permanently'}
              </button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}
