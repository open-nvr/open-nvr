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

// Vehicles — the first-class LPR vertical (the commercial surface).
//
// Capability-keyed, not app-keyed: this page appears when an enabled
// catalog app provides the license_plate_recognition capability
// (AppShell gates the nav entry the same way), so a community LPR app
// lights the same page. Data comes from the PLATFORM, not the app:
// plate reads are timeline visits (plate_text + best-frame evidence),
// written whichever producer ran the OCR. Watchlists write through the
// providing app's config endpoint — the same live-update path the
// catalog form uses.

import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Car, Download, RefreshCw, Search, ShieldAlert, ShieldCheck } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { AuthedImage } from '../components/AuthedImage'
import { Modal } from '../components/Modal'
import { useSnackbar } from '../components/Snackbar'
import {
  Badge, Button, Card, CardContent,
  EmptyState, ErrorCard, PageHeader, Skeleton,
} from '../components/ui'
import type { RegisteredApp } from './AppCatalog'

export const LPR_TASK = 'license_plate_recognition'

type PlateEvent = {
  id: number
  camera_id: number
  label?: string | null
  plate_text?: string | null
  started_at?: string | null
  ended_at?: string | null
  has_evidence?: boolean
  evidence_url?: string | null
}

type PlateStats = {
  days: number
  total_reads: number
  unique_plates: number
  per_camera: { camera_id: number; reads: number }[]
  per_day: { day: string; reads: number }[]
}

type CameraRow = { id: number; name: string }

/** The enabled app providing LPR — capability-keyed (community-proof). */
export function findLprApp(apps: RegisteredApp[] | undefined): RegisteredApp | null {
  return (
    (apps ?? []).find(
      (a) => a.enabled && (a.manifest?.requires_tasks ?? []).includes(LPR_TASK)
    ) ?? null
  )
}

const RANGE_PRESETS = [
  { key: '24h', label: 'Last 24h', hours: 24 },
  { key: '7d', label: '7 days', hours: 24 * 7 },
  { key: '30d', label: '30 days', hours: 24 * 30 },
] as const

function toCsv(rows: PlateEvent[], cameraName: (id: number) => string): string {
  const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
  const head = 'plate,camera,seen_at,left_at,label'
  const body = rows.map((r) =>
    [r.plate_text, cameraName(r.camera_id), r.started_at, r.ended_at, r.label]
      .map(esc).join(','))
  return [head, ...body].join('\n')
}

export function Vehicles() {
  const queryClient = useQueryClient()
  const { showSuccess, showError } = useSnackbar()
  const [plate, setPlate] = useState('')
  const [cameraId, setCameraId] = useState<number | ''>('')
  const [range, setRange] = useState<(typeof RANGE_PRESETS)[number]>(RANGE_PRESETS[0])
  const [preview, setPreview] = useState<PlateEvent | null>(null)

  const camerasQuery = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const { data } = await apiService.getCameras()
      const list = Array.isArray(data) ? data : (data as any)?.cameras
      return (Array.isArray(list) ? list : []) as CameraRow[]
    },
    retry: 0,
  })
  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
  })

  const fromIso = useMemo(
    () => new Date(Date.now() - range.hours * 3600 * 1000).toISOString(),
    // Re-anchor when the preset changes; a stable anchor per selection
    // keeps the query key stable between refetch intervals.
    [range]
  )

  const eventsQuery = useQuery({
    queryKey: ['plate-events', plate, cameraId, range.key],
    queryFn: async () => {
      const { data } = await apiService.getPlateEvents({
        plate: plate.trim() || undefined,
        camera_id: cameraId === '' ? undefined : cameraId,
        from: fromIso,
        limit: 200,
      })
      return ((data as any)?.events ?? []) as PlateEvent[]
    },
    retry: 0,
    refetchInterval: 30_000,
  })

  const statsQuery = useQuery({
    queryKey: ['plate-stats'],
    queryFn: async () => {
      const { data } = await apiService.getPlateStats(7)
      return data as PlateStats
    },
    retry: 0,
    refetchInterval: 60_000,
  })

  const lprApp = findLprApp(appsQuery.data)
  const allow: string[] = (lprApp?.config as any)?.allowlist ?? []
  const deny: string[] = (lprApp?.config as any)?.denylist ?? []

  // Add-to-watchlist writes through the providing app's config endpoint
  // (the app applies watchlists LIVE — the same path the catalog form
  // uses), merging over the current config so nothing else is lost.
  const watchlist = useMutation({
    mutationFn: async ({ plateText, list }: { plateText: string; list: 'allowlist' | 'denylist' }) => {
      if (!lprApp) throw new Error('No enabled LPR app to hold the watchlist.')
      const cfg = { ...(lprApp.config ?? {}) } as Record<string, any>
      const current: string[] = Array.isArray(cfg[list]) ? cfg[list] : []
      if (current.includes(plateText)) return
      cfg[list] = [...current, plateText]
      await apiService.updateAppConfig(lprApp.id, cfg)
    },
    onSuccess: (_d, vars) => {
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      showSuccess(`${vars.plateText} added to the ${vars.list}`)
    },
    onError: (e) => showError(extractApiError(e, 'Could not update the watchlist.')),
  })

  const cameraName = (id: number) =>
    camerasQuery.data?.find((c) => c.id === id)?.name ?? `cam${id}`

  const exportCsv = () => {
    const rows = eventsQuery.data ?? []
    const blob = new Blob([toCsv(rows, cameraName)], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `plate-reads-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  const stats = statsQuery.data
  const events = eventsQuery.data ?? []

  return (
    <section className="space-y-4">
      <PageHeader
        title="Vehicles"
        description="License plate reads across your cameras — searched from the evidence store, whichever part of the platform ran the OCR. Watchlists apply live."
        actions={
          <div className="flex items-center gap-2">
            <Button variant="outline" onClick={exportCsv} disabled={!events.length}>
              <Download size={14} /> Export CSV
            </Button>
            <Button onClick={() => eventsQuery.refetch()} disabled={eventsQuery.isFetching}>
              <RefreshCw size={14} className={eventsQuery.isFetching ? 'animate-spin' : ''} /> Refresh
            </Button>
          </div>
        }
      />

      {/* ── Stat tiles (7-day window) ─────────────────────────────── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {[
          { label: 'Reads (7d)', value: stats?.total_reads },
          { label: 'Unique plates (7d)', value: stats?.unique_plates },
          { label: 'Busiest camera (7d)', value: stats?.per_camera?.length
              ? cameraName([...stats.per_camera].sort((a, b) => b.reads - a.reads)[0].camera_id)
              : '—' },
          { label: 'Watchlist size', value: lprApp ? allow.length + deny.length : '—' },
        ].map((t) => (
          <Card key={t.label}>
            <CardContent className="py-3">
              <div className="text-2xl font-semibold">{t.value ?? '…'}</div>
              <div className="text-xs text-[var(--text-dim)]">{t.label}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* ── Filters ───────────────────────────────────────────────── */}
      <Card>
        <CardContent className="py-3 flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-[var(--text-dim)]" />
            <input
              value={plate}
              onChange={(e) => setPlate(e.target.value)}
              placeholder="Plate contains… (e.g. 1234)"
              className="pl-7 pr-2 py-1.5 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm w-56"
            />
          </div>
          <select
            value={cameraId}
            onChange={(e) => setCameraId(e.target.value === '' ? '' : Number(e.target.value))}
            className="py-1.5 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm"
          >
            <option value="">All cameras</option>
            {(camerasQuery.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
          <div className="flex rounded border border-[var(--border)] overflow-hidden">
            {RANGE_PRESETS.map((r) => (
              <button
                key={r.key}
                onClick={() => setRange(r)}
                className={`px-3 py-1.5 text-sm ${range.key === r.key
                  ? 'bg-[var(--accent,var(--bg-2))] text-white'
                  : 'bg-[var(--bg-2)] text-[var(--text-dim)]'}`}
              >
                {r.label}
              </button>
            ))}
          </div>
          {!lprApp && (
            <span className="text-xs text-[var(--text-dim)] ml-auto">
              No enabled LPR app — reads still collect; watchlists need the app.
            </span>
          )}
        </CardContent>
      </Card>

      {/* ── Reads table ───────────────────────────────────────────── */}
      {eventsQuery.isPending ? (
        <Skeleton className="h-64" />
      ) : eventsQuery.isError ? (
        <ErrorCard
          title="Could not load plate reads"
          message={extractApiError(eventsQuery.error, 'The events store is unreachable.')}
          onRetry={() => eventsQuery.refetch()}
        />
      ) : events.length === 0 ? (
        <EmptyState
          icon={<Car size={28} />}
          title="No plate reads in this window"
          description="Assign cameras the License Plate Recognition skill (Cameras → edit → Assignments) and vehicle visits will appear here with their evidence photos."
        />
      ) : (
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-[var(--text-dim)] border-b border-[var(--border)]">
                  <th className="px-3 py-2">Photo</th>
                  <th className="px-3 py-2">Plate</th>
                  <th className="px-3 py-2">Camera</th>
                  <th className="px-3 py-2">Seen</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2 text-right pr-4">Watchlist</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e) => {
                  const p = (e.plate_text ?? '').toUpperCase()
                  const inDeny = deny.includes(p)
                  const inAllow = allow.includes(p)
                  return (
                    <tr key={e.id} className="border-b border-[var(--border)] last:border-0 hover:bg-[var(--bg-2)]">
                      <td className="px-3 py-1.5">
                        {e.has_evidence ? (
                          <AuthedImage
                            queryKey={['evidence', e.id]}
                            fetchBlob={() => apiService.getEventEvidence(e.id)}
                            alt={`evidence for ${p}`}
                            className="h-10 w-16 object-cover rounded cursor-zoom-in"
                            onClick={() => setPreview(e)}
                          />
                        ) : (
                          <div className="h-10 w-16 grid place-items-center text-[10px] text-[var(--text-dim)] bg-[var(--bg-2)] rounded">—</div>
                        )}
                      </td>
                      <td className="px-3 py-1.5 font-mono font-semibold">{p}</td>
                      <td className="px-3 py-1.5">{cameraName(e.camera_id)}</td>
                      <td className="px-3 py-1.5 text-[var(--text-dim)]">
                        {e.started_at ? new Date(e.started_at).toLocaleString() : '—'}
                      </td>
                      <td className="px-3 py-1.5">
                        {inDeny ? <Badge variant="destructive">watchlist</Badge>
                          : inAllow ? <Badge variant="success">expected</Badge>
                          : <Badge variant="neutral">{e.label || 'vehicle'}</Badge>}
                      </td>
                      <td className="px-3 py-1.5 text-right pr-4 whitespace-nowrap">
                        {lprApp && !inDeny && (
                          <button
                            title="Alert at high severity when this plate is seen"
                            className="text-[var(--text-dim)] hover:text-[var(--text)] mr-2"
                            onClick={() => watchlist.mutate({ plateText: p, list: 'denylist' })}
                          >
                            <ShieldAlert size={15} />
                          </button>
                        )}
                        {lprApp && !inAllow && (
                          <button
                            title="Mark as an expected vehicle (low-severity reads)"
                            className="text-[var(--text-dim)] hover:text-[var(--text)]"
                            onClick={() => watchlist.mutate({ plateText: p, list: 'allowlist' })}
                          >
                            <ShieldCheck size={15} />
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}

      {/* ── Evidence preview ──────────────────────────────────────── */}
      {preview && (
        <Modal open onClose={() => setPreview(null)} title={`${(preview.plate_text ?? '').toUpperCase()} — ${cameraName(preview.camera_id)}`}>
          <AuthedImage
            queryKey={['evidence', preview.id]}
            fetchBlob={() => apiService.getEventEvidence(preview.id)}
            alt="evidence"
            className="max-h-[70vh] w-auto rounded"
          />
          <div className="text-xs text-[var(--text-dim)] mt-2">
            {preview.started_at ? new Date(preview.started_at).toLocaleString() : ''}
          </div>
        </Modal>
      )}
    </section>
  )
}
