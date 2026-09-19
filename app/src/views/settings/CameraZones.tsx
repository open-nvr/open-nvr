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

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSnackbar } from '../../components/Snackbar'
import { Button, ErrorCard, Skeleton } from '../../components/ui'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'
import { usePermissions } from '../../hooks/usePermissions'
import { useTranslation } from '../../i18n'
import { GeometryEditor, type GeometryReference } from '../apps/GeometryEditor'

type Pt = [number, number]
type Zone = { id: number; camera_id: number; name: string; polygon: Pt[]; labels: string[] | null }
type Camera = { id: number; name: string }
type Draft = { id: number | null; name: string; polygon: Pt[]; labels: string }

const inputCls = 'w-full bg-[var(--panel)] border border-[var(--border)] px-3 py-2 rounded text-sm'

/**
 * Named areas of a camera's picture ("driveway", "front door"). Detection
 * visits are tagged with the zones they cross, and Home Assistant shows a
 * per-zone occupancy sensor for each. Drawn on a live snapshot.
 */
export function CameraZones() {
  const { t } = useTranslation()
  const { hasPermission } = usePermissions()
  const canEdit = hasPermission('cameras.manage')
  const { showSuccess, showError } = useSnackbar()
  const [cameras, setCameras] = useState<Camera[]>([])
  const [cameraId, setCameraId] = useState<number | null>(null)
  const [zones, setZones] = useState<Zone[]>([])
  const [draft, setDraft] = useState<Draft | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    apiService.getCameras({})
      .then(({ data }) => {
        const list = (Array.isArray(data) ? data : data?.cameras || []) as Camera[]
        setCameras(list.map((c) => ({ id: c.id, name: c.name })))
        if (list.length) setCameraId(list[0].id)
        else setLoading(false)
      })
      .catch((e) => {
        setError(extractApiError(e, t('zones.failedLoad')))
        setLoading(false)
      })
  }, [t])

  const load = useCallback(async (cam: number) => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.listCameraZones(cam)
      setZones(data.zones || [])
    } catch (e: any) {
      setError(extractApiError(e, t('zones.failedLoad')))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    if (cameraId != null) {
      setDraft(null)
      load(cameraId)
    }
  }, [cameraId, load])

  // The editor keeps a per-camera JSON map; this panel edits one camera.
  const editorValue = useMemo(
    () => (draft && cameraId != null ? JSON.stringify({ [cameraId]: draft.polygon }) : '{}'),
    [draft, cameraId],
  )
  const onEditorChange = (json: string) => {
    if (!draft || cameraId == null) return
    try {
      const poly = JSON.parse(json)?.[String(cameraId)]
      setDraft({ ...draft, polygon: Array.isArray(poly) ? poly : [] })
    } catch {
      /* keep the last good shape */
    }
  }
  const references: GeometryReference[] = zones
    .filter((z) => z.id !== draft?.id)
    .map((z) => ({ label: z.name, value: z.polygon }))

  const save = async () => {
    if (!draft || cameraId == null) return
    const labels = draft.labels.split(',').map((s) => s.trim().toLowerCase()).filter(Boolean)
    const body = { name: draft.name.trim(), polygon: draft.polygon, labels: labels.length ? labels : null }
    setBusy(true)
    try {
      if (draft.id == null) await apiService.createCameraZone(cameraId, body)
      else await apiService.updateCameraZone(cameraId, draft.id, body)
      showSuccess(t('zones.saved', { name: body.name }))
      setDraft(null)
      await load(cameraId)
    } catch (e: any) {
      showError(extractApiError(e, t('zones.failedSave')))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (zone: Zone) => {
    if (cameraId == null || !window.confirm(t('zones.confirmDelete', { name: zone.name }))) return
    setBusy(true)
    try {
      await apiService.deleteCameraZone(cameraId, zone.id)
      if (draft?.id === zone.id) setDraft(null)
      await load(cameraId)
    } catch (e: any) {
      showError(extractApiError(e, t('zones.failedSave')))
    } finally {
      setBusy(false)
    }
  }

  if (!cameras.length && !loading) {
    return <p className="text-sm text-[var(--text-dim)]">{t('zones.noCameras')}</p>
  }

  const canSave = !!draft && draft.name.trim().length > 0 && draft.polygon.length >= 3 && !busy

  return (
    <div className="space-y-4">
      <p className="text-xs text-[var(--text-dim)] max-w-2xl">{t('zones.description')}</p>
      <label className="block max-w-sm space-y-1">
        <span className="text-sm">{t('zones.camera')}</span>
        <select
          className={inputCls}
          value={cameraId ?? ''}
          onChange={(e) => setCameraId(Number(e.target.value))}
        >
          {cameras.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </label>

      {loading ? <Skeleton className="h-32 w-full" /> : error ? (
        <ErrorCard message={error} onRetry={() => cameraId != null && load(cameraId)} />
      ) : (
        <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
          <div className="space-y-2">
            {zones.length === 0 && <p className="text-sm text-[var(--text-dim)]">{t('zones.empty')}</p>}
            {zones.map((z) => (
              <div
                key={z.id}
                data-testid="zone-row"
                className={`flex items-center justify-between gap-2 border rounded px-3 py-2 text-sm ${
                  draft?.id === z.id ? 'border-[var(--accent)]' : 'border-[var(--border)]'}`}
              >
                <div className="min-w-0">
                  <div className="truncate">{z.name}</div>
                  <div className="text-xs text-[var(--text-dim)] truncate">
                    {z.labels?.length ? z.labels.join(', ') : t('zones.allObjects')}
                  </div>
                </div>
                {canEdit && (
                  <div className="flex gap-1 shrink-0">
                    <Button size="sm" onClick={() => setDraft({ id: z.id, name: z.name, polygon: z.polygon, labels: (z.labels || []).join(', ') })}>
                      {t('zones.edit')}
                    </Button>
                    <Button size="sm" variant="danger" disabled={busy} onClick={() => remove(z)}>
                      {t('zones.delete')}
                    </Button>
                  </div>
                )}
              </div>
            ))}
            {canEdit && !draft && (
              <Button variant="primary" onClick={() => setDraft({ id: null, name: '', polygon: [], labels: '' })}>
                {t('zones.add')}
              </Button>
            )}
          </div>

          {draft && cameraId != null && (
            <div className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block space-y-1">
                  <span className="text-sm">{t('zones.name')}</span>
                  <input className={inputCls} maxLength={60} value={draft.name}
                         placeholder={t('zones.namePlaceholder')}
                         onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
                </label>
                <label className="block space-y-1">
                  <span className="text-sm">{t('zones.labels')}</span>
                  <input className={inputCls} value={draft.labels} placeholder="person, car"
                         onChange={(e) => setDraft({ ...draft, labels: e.target.value })} />
                </label>
              </div>
              <p className="text-xs text-[var(--text-dim)]">{t('zones.drawHint')}</p>
              <GeometryEditor
                kind="polygon"
                cameraId={cameraId}
                value={editorValue}
                onChange={onEditorChange}
                references={references}
              />
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={() => setDraft(null)}>{t('zones.cancel')}</Button>
                <Button variant="primary" disabled={!canSave} onClick={save}>{t('zones.save')}</Button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
