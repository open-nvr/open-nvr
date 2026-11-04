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
import { useSnackbar } from '../../components/Snackbar'
import { Button, ErrorCard, Skeleton } from '../../components/ui'
import { useConfirm } from '../../components/ui/ConfirmDialog'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'
import { usePermissions } from '../../hooks/usePermissions'
import { useTranslation } from '../../i18n'
import type { DetectionSettings, ObjectFilter } from '../../services/cameraService'

type Camera = { id: number; name: string }

/** The form keeps every number as text so a half-typed value survives;
 *  '' means "inherit the system value". Areas are shown as % of the frame
 *  and stored as fractions. */
type FilterRow = { label: string; min_area: string; max_area: string; min_ratio: string; max_ratio: string; min_score: string }
type Form = {
  fps: string
  motion_enabled: '' | 'true' | 'false'
  motion_threshold: string
  motion_contour_area: string
  min_spawn_score: string
  stationary_interval: string
  detect_stream: '' | 'auto' | 'sub' | 'main'
  filters: FilterRow[]
}

const inputCls = 'w-full bg-[var(--panel)] border border-[var(--border)] px-3 py-2  text-sm'
const cellCls = 'w-full bg-[var(--panel)] border border-[var(--border)] px-2 py-1 text-sm'
const LABEL_RE = /^[a-z0-9][a-z0-9 _.-]{0,59}$/

const numStr = (v: number | null | undefined, scale = 1) => (v == null ? '' : String(Math.round(v * scale * 1e6) / 1e6))
const numOrNull = (s: string, scale = 1): number | null => {
  const t = s.trim()
  if (!t) return null
  const n = Number(t)
  return Number.isFinite(n) ? n / scale : null
}

function toForm(d: DetectionSettings | null | undefined): Form {
  const filters = Object.entries(d?.object_filters || {}).map(([label, f]) => ({
    label,
    min_area: numStr(f.min_area, 100),
    max_area: numStr(f.max_area, 100),
    min_ratio: numStr(f.min_ratio),
    max_ratio: numStr(f.max_ratio),
    min_score: numStr(f.min_score),
  }))
  return {
    fps: numStr(d?.fps),
    motion_enabled: d?.motion_enabled == null ? '' : d.motion_enabled ? 'true' : 'false',
    motion_threshold: numStr(d?.motion_threshold),
    motion_contour_area: numStr(d?.motion_contour_area),
    min_spawn_score: numStr(d?.min_spawn_score),
    stationary_interval: numStr(d?.stationary_interval),
    detect_stream: (d?.detect_stream ?? '') as Form['detect_stream'],
    filters,
  }
}

function toBody(f: Form): DetectionSettings {
  const object_filters: Record<string, ObjectFilter> = {}
  for (const r of f.filters) {
    const label = r.label.trim().toLowerCase()
    if (!label) continue
    object_filters[label] = {
      min_area: numOrNull(r.min_area, 100),
      max_area: numOrNull(r.max_area, 100),
      min_ratio: numOrNull(r.min_ratio),
      max_ratio: numOrNull(r.max_ratio),
      min_score: numOrNull(r.min_score),
    }
  }
  return {
    fps: numOrNull(f.fps),
    motion_enabled: f.motion_enabled === '' ? null : f.motion_enabled === 'true',
    motion_threshold: numOrNull(f.motion_threshold),
    motion_contour_area: numOrNull(f.motion_contour_area),
    min_spawn_score: numOrNull(f.min_spawn_score),
    stationary_interval: numOrNull(f.stationary_interval),
    detect_stream: f.detect_stream === '' ? null : f.detect_stream,
    object_filters: Object.keys(object_filters).length ? object_filters : null,
  }
}

const EMPTY_ROW: FilterRow = { label: '', min_area: '', max_area: '', min_ratio: '', max_ratio: '', min_score: '' }

/**
 * A camera's overrides of the detect-pipeline's system-wide knobs: analysis
 * rate, motion gate, spawn score, stationary re-check, and per-label object
 * filters. Empty = inherit. Saved as one record; the camera's detection
 * worker restarts on the pipeline's next reconcile tick.
 */
export function CameraDetectionSettings() {
  const confirm = useConfirm()
  const { t } = useTranslation()
  const { hasPermission } = usePermissions()
  const canEdit = hasPermission('cameras.manage')
  const { showSuccess, showError } = useSnackbar()
  const [cameras, setCameras] = useState<Camera[]>([])
  const [cameraId, setCameraId] = useState<number | null>(null)
  const [form, setForm] = useState<Form>(toForm(null))
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
        setError(extractApiError(e, t('detect.failedLoad')))
        setLoading(false)
      })
  }, [t])

  const load = useCallback(async (cam: number) => {
    setLoading(true)
    setError('')
    try {
      const { data } = await apiService.getDetectionSettings(cam)
      setForm(toForm(data.detection))
    } catch (e: unknown) {
      setError(extractApiError(e, t('detect.failedLoad')))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    if (cameraId != null) load(cameraId)
  }, [cameraId, load])

  const badLabel = form.filters.some((r) => r.label.trim() && !LABEL_RE.test(r.label.trim().toLowerCase()))
  const canSave = canEdit && !busy && !badLabel

  const save = async (body: DetectionSettings) => {
    if (cameraId == null) return
    setBusy(true)
    try {
      const { data } = await apiService.putDetectionSettings(cameraId, body)
      setForm(toForm(data.detection))
      showSuccess(t('detect.saved'))
    } catch (e: unknown) {
      showError(extractApiError(e, t('detect.failedSave')))
    } finally {
      setBusy(false)
    }
  }

  const reset = async () => {
    if (!(await confirm({ title: t('detect.confirmReset'), confirmLabel: t('detect.reset'), danger: true }))) return
    await save({})
  }

  const set = (patch: Partial<Form>) => setForm((f) => ({ ...f, ...patch }))
  const setRow = (i: number, patch: Partial<FilterRow>) =>
    setForm((f) => ({ ...f, filters: f.filters.map((r, j) => (j === i ? { ...r, ...patch } : r)) }))

  if (!cameras.length && !loading) {
    return <p className="text-sm text-[var(--text-dim)]">{t('detect.noCameras')}</p>
  }

  const numberField = (key: keyof Omit<Form, 'filters' | 'motion_enabled'>, label: string, hint: string, step = '1') => (
    <label className="block space-y-1">
      <span className="text-sm">{label}</span>
      <input
        className={inputCls}
        type="number"
        step={step}
        placeholder={t('detect.inherit')}
        value={form[key]}
        disabled={!canEdit}
        onChange={(e) => set({ [key]: e.target.value } as Partial<Form>)}
      />
      <span className="block text-xs text-[var(--text-dim)]">{hint}</span>
    </label>
  )

  return (
    <div className="space-y-4">
      <p className="text-xs text-[var(--text-dim)] max-w-2xl">{t('detect.description')}</p>
      <label className="block max-w-sm space-y-1">
        <span className="text-sm">{t('detect.camera')}</span>
        <select className={inputCls} value={cameraId ?? ''} onChange={(e) => setCameraId(Number(e.target.value))}>
          {cameras.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </label>

      {loading ? <Skeleton className="h-32 w-full" /> : error ? (
        <ErrorCard message={error} onRetry={() => cameraId != null && load(cameraId)} />
      ) : (
        <div className="space-y-5 max-w-3xl">
          <div className="grid gap-4 sm:grid-cols-2">
            {numberField('fps', t('detect.fps'), t('detect.fpsHint'))}
            <label className="block space-y-1">
              <span className="text-sm">{t('detect.motionEnabled')}</span>
              <select className={inputCls} value={form.motion_enabled} disabled={!canEdit}
                      onChange={(e) => set({ motion_enabled: e.target.value as Form['motion_enabled'] })}>
                <option value="">{t('detect.inherit')}</option>
                <option value="true">{t('detect.on')}</option>
                <option value="false">{t('detect.off')}</option>
              </select>
              <span className="block text-xs text-[var(--text-dim)]">{t('detect.motionEnabledHint')}</span>
            </label>
            {numberField('motion_threshold', t('detect.motionThreshold'), t('detect.motionThresholdHint'))}
            {numberField('motion_contour_area', t('detect.contourArea'), t('detect.contourAreaHint'))}
            {numberField('min_spawn_score', t('detect.minSpawnScore'), t('detect.minSpawnScoreHint'), '0.05')}
            {numberField('stationary_interval', t('detect.stationary'), t('detect.stationaryHint'))}
            <label className="block space-y-1">
              <span className="text-sm">{t('detect.detectStream')}</span>
              <select className={inputCls} value={form.detect_stream} disabled={!canEdit}
                      onChange={(e) => set({ detect_stream: e.target.value as Form['detect_stream'] })}>
                <option value="">{t('detect.inherit')}</option>
                <option value="auto">{t('detect.streamAuto')}</option>
                <option value="sub">{t('detect.streamSub')}</option>
                <option value="main">{t('detect.streamMain')}</option>
              </select>
              <span className="block text-xs text-[var(--text-dim)]">{t('detect.detectStreamHint')}</span>
            </label>
          </div>

          <div className="space-y-2">
            <div className="text-sm">{t('detect.filters')}</div>
            <p className="text-xs text-[var(--text-dim)]">{t('detect.filtersHint')}</p>
            {form.filters.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="text-xs text-[var(--text-dim)]">
                    <tr>
                      <th className="text-left font-normal pr-2">{t('detect.label')}</th>
                      <th className="text-left font-normal pr-2">{t('detect.minArea')}</th>
                      <th className="text-left font-normal pr-2">{t('detect.maxArea')}</th>
                      <th className="text-left font-normal pr-2">{t('detect.minRatio')}</th>
                      <th className="text-left font-normal pr-2">{t('detect.maxRatio')}</th>
                      <th className="text-left font-normal pr-2">{t('detect.minScore')}</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {form.filters.map((r, i) => (
                      <tr key={i} data-testid="filter-row">
                        <td className="pr-2 py-1">
                          <input className={cellCls} value={r.label} placeholder="person" disabled={!canEdit}
                                 onChange={(e) => setRow(i, { label: e.target.value })} />
                        </td>
                        {(['min_area', 'max_area', 'min_ratio', 'max_ratio', 'min_score'] as const).map((k) => (
                          <td key={k} className="pr-2 py-1">
                            <input className={cellCls} type="number" step="any" value={r[k]} disabled={!canEdit}
                                   onChange={(e) => setRow(i, { [k]: e.target.value })} />
                          </td>
                        ))}
                        <td className="py-1">
                          {canEdit && (
                            <Button size="sm" variant="danger"
                                    onClick={() => set({ filters: form.filters.filter((_, j) => j !== i) })}>
                              {t('detect.removeFilter')}
                            </Button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {badLabel && <p className="text-xs text-[var(--danger,#c00)]">{t('detect.badLabel')}</p>}
            {canEdit && (
              <Button size="sm" onClick={() => set({ filters: [...form.filters, { ...EMPTY_ROW }] })}>
                {t('detect.addFilter')}
              </Button>
            )}
          </div>

          {canEdit && (
            <div className="flex justify-end gap-2">
              <Button variant="outline" disabled={busy} onClick={reset}>{t('detect.reset')}</Button>
              <Button variant="primary" disabled={!canSave} onClick={() => save(toBody(form))}>{t('detect.save')}</Button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
