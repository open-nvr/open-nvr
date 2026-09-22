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

import { useCallback, useEffect, useState } from 'react'
import { PauseCircle } from 'lucide-react'
import { useSnackbar } from '../../components/Snackbar'
import { Button } from '../../components/ui'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'
import { usePermissions } from '../../hooks/usePermissions'
import { useTranslation, useDateFormat, type DateFormatters } from '../../i18n'

type PauseInfo = { since: string; resume_at: string | null; by: string; reason: string | null }

/**
 * The site-wide opt-in that lets recording be paused (off by default: this is
 * a recorder), and the cameras paused right now with a Resume button each.
 * Superuser only; the server enforces the same.
 */
export function RecordingPauseSetting() {
  const fmt = useDateFormat()
  const { t } = useTranslation()
  const { isSuperuser } = usePermissions()
  const { showSuccess, showError } = useSnackbar()
  const [enabled, setEnabled] = useState(false)
  const [paused, setPaused] = useState<Record<string, PauseInfo>>({})
  const [names, setNames] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState('')
  const [loaded, setLoaded] = useState(false)

  const load = useCallback(async () => {
    try {
      const [setting, cams] = await Promise.all([
        apiService.getRecordingPauseSetting(),
        apiService.getCameras({}),
      ])
      setEnabled(!!setting.data.enabled)
      setPaused(setting.data.paused || {})
      const list = Array.isArray(cams.data) ? cams.data : cams.data?.cameras || []
      setNames(Object.fromEntries(list.map((c: any) => [String(c.id), c.name])))
      setLoaded(true)
    } catch (e: any) {
      showError(extractApiError(e, t('recordingPause.failedLoad')))
    }
  }, [showError, t])

  useEffect(() => {
    if (isSuperuser) load()
  }, [isSuperuser, load])

  if (!isSuperuser || !loaded) return null

  const toggle = async () => {
    const next = !enabled
    if (!next || window.confirm(t('recordingPause.confirmEnable'))) {
      setBusy('toggle')
      try {
        const { data } = await apiService.setRecordingPauseSetting(next)
        setEnabled(!!data.enabled)
        setPaused(data.paused || {})
        showSuccess(next ? t('recordingPause.enabled') : t('recordingPause.disabled', { count: (data.resumed_cameras || []).length }))
      } catch (e: any) {
        showError(extractApiError(e, t('recordingPause.failedSave')))
      } finally {
        setBusy('')
      }
    }
  }

  const resume = async (cameraId: string) => {
    setBusy(cameraId)
    try {
      await apiService.setCameraRecording(Number(cameraId), { enabled: true, reason: 'resumed from Settings' })
      await load()
    } catch (e: any) {
      showError(extractApiError(e, t('recordingPause.failedResume')))
    } finally {
      setBusy('')
    }
  }

  const entries = Object.entries(paused)

  return (
    <div className="bg-[var(--panel)] border border-neutral-800 rounded-lg p-6 mb-8 space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-lg font-medium flex items-center gap-2">
            <PauseCircle size={20} />
            {t('recordingPause.title')}
          </h3>
          <p className="text-xs text-[var(--text-dim)] mt-1 max-w-xl">{t('recordingPause.description')}</p>
        </div>
        <label className="flex items-center gap-2 text-sm shrink-0">
          <input
            type="checkbox"
            data-testid="recording-pause-allowed"
            className="accent-[var(--accent)]"
            checked={enabled}
            disabled={busy === 'toggle'}
            onChange={toggle}
          />
          {t('recordingPause.allow')}
        </label>
      </div>

      {entries.length > 0 && (
        <div className="space-y-2">
          <div className="text-sm font-medium">{t('recordingPause.pausedNow')}</div>
          {entries.map(([camId, info]) => (
            <div key={camId} className="flex items-center justify-between gap-3 border border-[var(--border)] rounded px-3 py-2 text-sm">
              <div>
                <div>{names[camId] ?? `#${camId}`}</div>
                <div className="text-xs text-[var(--text-dim)]">
                  {t('recordingPause.pausedBy', { by: info.by, since: fmt.dateTime(info.since) })}
                  {info.resume_at && <> · {t('recordingPause.resumesAt', { at: fmt.dateTime(info.resume_at) })}</>}
                  {info.reason && <> · {info.reason}</>}
                </div>
              </div>
              <Button variant="primary" disabled={busy === camId} onClick={() => resume(camId)}>
                {t('recordingPause.resume')}
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
