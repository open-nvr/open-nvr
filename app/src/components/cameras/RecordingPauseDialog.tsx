// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Modal } from '../Modal'
import { Button } from '../ui'
import { Field, Input, Select } from '../ui/form'
import { useSnackbar } from '../Snackbar'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'
import { useTranslation } from '../../i18n'

/** How long a pause lasts. '' = until someone resumes it; the server caps an
 *  automatic resume at 7 days. */
const DURATIONS: Array<{ value: string; key: string }> = [
  { value: '900', key: 'recordingPause.for15m' },
  { value: '3600', key: 'recordingPause.for1h' },
  { value: '28800', key: 'recordingPause.for8h' },
  { value: '86400', key: 'recordingPause.for24h' },
  { value: '', key: 'recordingPause.untilResumed' },
]

/**
 * Pause recording on one camera or a selection of them. OpenNVR records all
 * the time, so this only opens where a site admin has allowed pausing; an
 * admin who opens it while pausing is off is pointed at the setting instead.
 */
export function RecordingPauseDialog({
  cameras, allowed, onClose, onDone,
}: {
  cameras: Array<{ id: number; name: string }>
  /** The site's "Allow pausing recording" setting. */
  allowed: boolean
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { showSuccess, showError } = useSnackbar()
  const [duration, setDuration] = useState('3600')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const single = cameras.length === 1 ? cameras[0] : null

  const pause = async () => {
    setBusy(true)
    const body = {
      enabled: false,
      ...(duration ? { resume_after_s: Number(duration) } : {}),
      ...(reason.trim() ? { reason: reason.trim() } : {}),
    }
    let failed = 0
    let lastError: unknown = null
    for (const c of cameras) {
      try { await apiService.setCameraRecording(c.id, body) } catch (e) { failed++; lastError = e }
    }
    setBusy(false)
    onDone()
    if (single && failed) {
      showError(extractApiError(lastError as any, t('recordingPause.failedPause')))
      return
    }
    if (failed) showError(t('camera.bulkPartial', { failed, total: cameras.length }))
    else showSuccess(single
      ? t('recordingPause.pausedOk', { name: single.name })
      : t('recordingPause.pausedMany', { count: cameras.length }))
    onClose()
  }

  return (
    <Modal
      open
      title={single
        ? t('recordingPause.pauseTitle', { name: single.name })
        : t('recordingPause.pauseTitleMany', { count: cameras.length })}
      onClose={onClose}
      widthClassName="w-[420px]"
      data-testid="recording-pause-dialog"
    >
      {allowed ? (
        <div className="space-y-4">
          <p className="text-xs text-[var(--text-dim)]">{t('recordingPause.pauseHint')}</p>
          <Field label={t('recordingPause.duration')}>
            {(id) => (
              <Select id={id} value={duration} onChange={(e) => setDuration(e.target.value)} data-testid="recording-pause-duration">
                {DURATIONS.map((d) => <option key={d.key} value={d.value}>{t(d.key)}</option>)}
              </Select>
            )}
          </Field>
          <Field label={t('recordingPause.reason')}>
            {(id) => (
              <Input
                id={id}
                value={reason}
                maxLength={200}
                onChange={(e) => setReason(e.target.value)}
                placeholder={t('recordingPause.reasonPlaceholder')}
              />
            )}
          </Field>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={onClose}>{t('common.cancel')}</Button>
            <Button variant="primary" disabled={busy} onClick={pause} data-testid="recording-pause-confirm">
              {t('recordingPause.pause')}
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <p className="text-sm">{t('recordingPause.notAllowed')}</p>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={onClose}>{t('common.cancel')}</Button>
            <Button variant="primary" onClick={() => { onClose(); navigate('/settings/recording') }}>
              {t('recordingPause.openSettings')}
            </Button>
          </div>
        </div>
      )}
    </Modal>
  )
}
