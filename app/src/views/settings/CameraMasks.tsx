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
import type { CameraMaskKind } from '../../services/cameraService'
import { GeometryEditor, type GeometryReference } from '../apps/GeometryEditor'
import { useConfirm } from '../../components/ui/ConfirmDialog'

type Pt = [number, number]
type Mask = {
  id: number; camera_id: number; name: string; kind: CameraMaskKind; polygon: Pt[]; labels: string[] | null
}
type Zone = { id: number; name: string; polygon: Pt[] }
type Camera = { id: number; name: string }
type Draft = { id: number | null; name: string; kind: CameraMaskKind; polygon: Pt[]; labels: string }

const inputCls = 'w-full bg-[var(--panel)] border border-[var(--border)] px-3 py-2  text-sm'

/**
 * What Tier-0 detection must ignore in a camera's picture. A motion mask
 * blinds the motion gate (a burned-in clock, a tree, a road at the edge);
 * an object mask drops detections standing inside it (a TV, a poster, the
 * cars across the street). Drawn on a live snapshot; the camera's zones and
 * other masks are shown faintly for orientation.
 */
export function CameraMasks() {
  const confirm = useConfirm()
  const { t } = useTranslation()
  const { hasPermission } = usePermissions()
  const canEdit = hasPermission('cameras.manage')
  const { showSuccess, showError } = useSnackbar()
  const [cameras, setCameras] = useState<Camera[]>([])
  const [cameraId, setCameraId] = useState<number | null>(null)
  const [masks, setMasks] = useState<Mask[]>([])
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
        setError(extractApiError(e, t('masks.failedLoad')))
        setLoading(false)
      })
  }, [t])

  const load = useCallback(async (cam: number) => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.listCameraMasks(cam)
      setMasks(data.masks || [])
      // Zones are only context here; failing to load them is not an error.
      try {
        const z = await apiService.listCameraZones(cam)
        setZones(z.data.zones || [])
      } catch {
        setZones([])
      }
    } catch (e: any) {
      setError(extractApiError(e, t('masks.failedLoad')))
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
  const references: GeometryReference[] = [
    ...masks.filter((m) => m.id !== draft?.id).map((m) => ({ label: m.name, value: m.polygon })),
    ...zones.map((z) => ({ label: `${t('settings.zones')}: ${z.name}`, value: z.polygon })),
  ]

  const kindLabel = (kind: CameraMaskKind) => (kind === 'object' ? t('masks.kindObject') : t('masks.kindMotion'))

  const save = async () => {
    if (!draft || cameraId == null) return
    const labels = draft.labels.split(',').map((s) => s.trim().toLowerCase()).filter(Boolean)
    const body = {
      name: draft.name.trim(),
      kind: draft.kind,
      polygon: draft.polygon,
      labels: draft.kind === 'object' && labels.length ? labels : null,
    }
    setBusy(true)
    try {
      if (draft.id == null) await apiService.createCameraMask(cameraId, body)
      else await apiService.updateCameraMask(cameraId, draft.id, body)
      showSuccess(t('masks.saved', { name: body.name }))
      setDraft(null)
      await load(cameraId)
    } catch (e: any) {
      showError(extractApiError(e, t('masks.failedSave')))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (mask: Mask) => {
    if (cameraId == null || !(await confirm({ title: t('masks.confirmDelete', { name: mask.name }), confirmLabel: 'Delete', danger: true }))) return
    setBusy(true)
    try {
      await apiService.deleteCameraMask(cameraId, mask.id)
      if (draft?.id === mask.id) setDraft(null)
      await load(cameraId)
    } catch (e: any) {
      showError(extractApiError(e, t('masks.failedSave')))
    } finally {
      setBusy(false)
    }
  }

  if (!cameras.length && !loading) {
    return <p className="text-sm text-[var(--text-dim)]">{t('masks.noCameras')}</p>
  }

  const canSave = !!draft && draft.name.trim().length > 0 && draft.polygon.length >= 3 && !busy

  return (
    <div className="space-y-4">
      <p className="text-xs text-[var(--text-dim)] max-w-2xl">{t('masks.description')}</p>
      <label className="block max-w-sm space-y-1">
        <span className="text-sm">{t('masks.camera')}</span>
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
            {masks.length === 0 && <p className="text-sm text-[var(--text-dim)]">{t('masks.empty')}</p>}
            {masks.map((m) => (
              <div
                key={m.id}
                data-testid="mask-row"
                className={`flex items-center justify-between gap-2 border rounded px-3 py-2 text-sm ${
                  draft?.id === m.id ? 'border-[var(--accent)]' : 'border-[var(--border)]'}`}
              >
                <div className="min-w-0">
                  <div className="truncate">{m.name}</div>
                  <div className="text-xs text-[var(--text-dim)] truncate">
                    {kindLabel(m.kind)}
                    {m.kind === 'object' && ` · ${m.labels?.length ? m.labels.join(', ') : t('masks.allObjects')}`}
                  </div>
                </div>
                {canEdit && (
                  <div className="flex gap-1 shrink-0">
                    <Button size="sm" onClick={() => setDraft({ id: m.id, name: m.name, kind: m.kind, polygon: m.polygon, labels: (m.labels || []).join(', ') })}>
                      {t('masks.edit')}
                    </Button>
                    <Button size="sm" variant="danger" disabled={busy} onClick={() => remove(m)}>
                      {t('masks.delete')}
                    </Button>
                  </div>
                )}
              </div>
            ))}
            {canEdit && !draft && (
              <Button variant="primary" onClick={() => setDraft({ id: null, name: '', kind: 'motion', polygon: [], labels: '' })}>
                {t('masks.add')}
              </Button>
            )}
          </div>

          {draft && cameraId != null && (
            <div className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block space-y-1">
                  <span className="text-sm">{t('masks.name')}</span>
                  <input className={inputCls} maxLength={60} value={draft.name}
                         placeholder={t('masks.namePlaceholder')}
                         onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
                </label>
                <label className="block space-y-1">
                  <span className="text-sm">{t('masks.kind')}</span>
                  <select className={inputCls} value={draft.kind}
                          onChange={(e) => setDraft({ ...draft, kind: e.target.value as CameraMaskKind })}>
                    <option value="motion">{t('masks.kindMotion')}</option>
                    <option value="object">{t('masks.kindObject')}</option>
                  </select>
                </label>
                {draft.kind === 'object' && (
                  <label className="block space-y-1 sm:col-span-2">
                    <span className="text-sm">{t('masks.labels')}</span>
                    <input className={inputCls} value={draft.labels} placeholder="person, car"
                           onChange={(e) => setDraft({ ...draft, labels: e.target.value })} />
                  </label>
                )}
              </div>
              <p className="text-xs text-[var(--text-dim)]">{t('masks.drawHint')}</p>
              <GeometryEditor
                kind="polygon"
                cameraId={cameraId}
                value={editorValue}
                onChange={onEditorChange}
                references={references}
              />
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={() => setDraft(null)}>{t('masks.cancel')}</Button>
                <Button variant="primary" disabled={!canSave} onClick={save}>{t('masks.save')}</Button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
