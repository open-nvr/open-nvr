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

// Occupancy — the second first-class vertical, proving the pattern
// generalizes: capability-keyed on manifest `provides` ("occupancy"),
// so a community-built replacement app lights the same page. Live
// data comes from the providing app's /state through core's proxy;
// thresholds write through the same live config path as everything
// else. Zones are drawn in the catalog's geometry editor — this page
// links there rather than duplicating it.

import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Flame, RefreshCw, Settings2, Users } from 'lucide-react'
import { api } from '../lib/api'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { useSnackbar } from '../components/Snackbar'
import {
  Badge, Button, Card, CardContent,
  EmptyState, ErrorCard, PageHeader, Skeleton,
} from '../components/ui'
import { Modal } from '../components/Modal'
import type { RegisteredApp } from './AppCatalog'

export const OCCUPANCY_CAPABILITY = 'occupancy'

type CameraRow = { id: number; name: string }

type OccupancyCameraState = {
  level?: string        // over | under | normal | …
  last_count?: number
  pending?: number
}

type HistoryResp = {
  hours: number
  bucket_minutes: number
  cameras: { camera_id: number; samples: { t: string; avg: number; max: number }[] }[]
  busiest_hours: { hour: number; avg: number }[]
}

type OccupancyState = {
  total_people?: number
  zones_over?: number
  heatmap_enabled?: boolean
  heatmaps_published?: number
  cameras?: Record<string, OccupancyCameraState>
}

/** GET /occupancy/heatmap — a unit-space grid of where watched entities
 *  stood (foot points), summed over the window. */
type HeatmapResp = {
  camera_id: number
  hours: number
  cols: number
  rows: number
  cells: number[]
  max: number
  frames: number
  hours_covered: number
  updated_at: string | null
}

/** The enabled app providing occupancy — capability-keyed. */
export function findOccupancyApp(apps: RegisteredApp[] | undefined): RegisteredApp | null {
  return (
    (apps ?? []).find(
      (a) => a.enabled && ((a.manifest as any)?.provides ?? []).includes(OCCUPANCY_CAPABILITY)
    ) ?? null
  )
}

function levelBadge(level: string | undefined) {
  if (level === 'over') return <Badge variant="destructive">over limit</Badge>
  if (level === 'under') return <Badge variant="warning">under minimum</Badge>
  if (level === 'normal') return <Badge variant="success">normal</Badge>
  return <Badge variant="neutral">{level || 'counting…'}</Badge>
}

export function Occupancy() {
  const queryClient = useQueryClient()
  const { showSuccess, showError } = useSnackbar()

  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
  })
  const camerasQuery = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const { data } = await apiService.getCameras()
      const list = Array.isArray(data) ? data : (data as any)?.cameras
      return (Array.isArray(list) ? list : []) as CameraRow[]
    },
    retry: 0,
  })

  const occApp = findOccupancyApp(appsQuery.data)

  // History charts (occupancy.changed.v1 samples persisted by core).
  const historyQuery = useQuery({
    queryKey: ['occupancy-history'],
    queryFn: async () => {
      const { data } = await api.get('/api/v1/occupancy/history', { params: { hours: 24 } })
      return data as HistoryResp
    },
    enabled: Boolean(occApp),
    retry: 0,
    refetchInterval: 60_000,
  })
  const history = historyQuery.data

  const statusQuery = useQuery({
    queryKey: ['app-status', occApp?.id],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(occApp!.id)
      return data as { state?: OccupancyState }
    },
    enabled: Boolean(occApp),
    retry: 0,
    refetchInterval: 5000,
  })
  const state = statusQuery.data?.state

  // The app's zone states are keyed by the platform camera handle
  // ("cam3") — resolve to the operator's camera names, tolerantly.
  const cameraIdOf = (key: string) => {
    const m = /^cam(\d+)$/.exec(key)
    return m ? Number(m[1]) : Number(key)
  }
  const cameraName = (key: string) => {
    const id = cameraIdOf(key)
    return camerasQuery.data?.find((c) => c.id === id)?.name ?? key
  }
  const [heatmapFor, setHeatmapFor] = useState<string | null>(null)
  const watchLabels: string[] = Array.isArray((occApp?.config as any)?.watch_labels)
    ? (occApp!.config as any).watch_labels.map(String)
    : ['person']

  const maxOccupancy = Number((occApp?.config as any)?.max_occupancy ?? 0) || 0
  const minOccupancy = Number((occApp?.config as any)?.min_occupancy ?? 0) || 0
  const [draftMax, setDraftMax] = useState<string | null>(null)
  const [draftMin, setDraftMin] = useState<string | null>(null)

  const saveThresholds = useMutation({
    mutationFn: async () => {
      if (!occApp) throw new Error('No enabled occupancy app.')
      const cfg = { ...((occApp.config ?? {}) as Record<string, any>) }
      const nextMax = Number(draftMax ?? maxOccupancy)
      if (!Number.isFinite(nextMax) || nextMax < 1) {
        throw new Error('Max occupancy must be at least 1.')
      }
      cfg.max_occupancy = Math.floor(nextMax)
      const nextMin = Number(draftMin ?? minOccupancy)
      if (Number.isFinite(nextMin) && nextMin > 0) cfg.min_occupancy = Math.floor(nextMin)
      else delete cfg.min_occupancy
      await apiService.updateAppConfig(occApp.id, cfg)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      setDraftMax(null)
      setDraftMin(null)
      showSuccess('Thresholds saved — applied live')
    },
    onError: (e) => showError(extractApiError(e, 'Could not save thresholds.')),
  })

  const seriesFor = (key: string) => {
    const m = /^cam(\d+)$/.exec(key)
    const id = m ? Number(m[1]) : Number(key)
    return history?.cameras.find((c) => c.camera_id === id)?.samples ?? []
  }

  const zones = useMemo(
    () => Object.entries(state?.cameras ?? {}).sort(([a], [b]) => a.localeCompare(b)),
    [state]
  )

  if (!appsQuery.isPending && !occApp) {
    return (
      <section className="space-y-4">
        <PageHeader
          title="Occupancy"
          description="Live head-counts per watched zone, with over/under-occupancy alerts."
        />
        <EmptyState
          icon={<Users size={28} />}
          title="No occupancy app enabled"
          description="Install and enable Occupancy Counting from the App Catalog — it rides the detection stream the platform already produces, so it adds zero inference cost."
        />
      </section>
    )
  }

  return (
    <section className="space-y-4">
      <PageHeader
        title="Occupancy"
        description="Live head-counts per watched zone — riding the platform's detection stream, zero extra inference. Thresholds apply live."
        actions={
          <div className="flex items-center gap-2">
            {occApp && (
              <Link to={`/app-catalog/${occApp.id}`}>
                <Button variant="outline">
                  <Settings2 size={14} /> Configure zones
                </Button>
              </Link>
            )}
            <Button onClick={() => statusQuery.refetch()} disabled={statusQuery.isFetching}>
              <RefreshCw size={14} className={statusQuery.isFetching ? 'animate-spin' : ''} /> Refresh
            </Button>
          </div>
        }
      />

      {/* ── Live tiles ────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {[
          { label: 'People counted now', value: state?.total_people },
          { label: 'Zones over limit', value: state?.zones_over },
          { label: 'Zones watched', value: zones.length || undefined },
          { label: 'Max per zone', value: maxOccupancy || '—' },
        ].map((t) => (
          <Card key={t.label}>
            <CardContent className="py-3">
              <div className="text-2xl font-semibold">{t.value ?? '…'}</div>
              <div className="text-xs text-[var(--text-dim)]">{t.label}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* ── Thresholds (applied live) ─────────────────────────────── */}
      <Card>
        <CardContent className="py-3 flex flex-wrap items-end gap-3">
          <label className="text-xs text-[var(--text-dim)]">
            Max occupancy (alert when over)
            <input
              type="number"
              min={1}
              value={draftMax ?? String(maxOccupancy || '')}
              onChange={(e) => setDraftMax(e.target.value)}
              className="block mt-0.5 py-1.5 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm text-[var(--text)] w-40"
            />
          </label>
          <label className="text-xs text-[var(--text-dim)]">
            Min occupancy (0 = off)
            <input
              type="number"
              min={0}
              value={draftMin ?? String(minOccupancy || 0)}
              onChange={(e) => setDraftMin(e.target.value)}
              className="block mt-0.5 py-1.5 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm text-[var(--text)] w-40"
            />
          </label>
          <Button
            onClick={() => saveThresholds.mutate()}
            disabled={saveThresholds.isPending || !occApp || (draftMax === null && draftMin === null)}
          >
            Save thresholds
          </Button>
          <span className="text-xs text-[var(--text-dim)] ml-auto">
            Zones are drawn per camera in the app's Configure form.
          </span>
        </CardContent>
      </Card>

      {/* ── Busiest hours (7 days of occupancy.changed.v1 samples) ── */}
      {(history?.busiest_hours?.length ?? 0) > 0 && (
        <Card>
          <CardContent className="py-3">
            <div className="text-sm font-medium mb-0.5">Busiest hours</div>
            <div className="text-xs text-[var(--text-dim)] mb-2">
              Average head-count by hour of day, last 7 days — staff the peaks.
            </div>
            <BusiestHoursChart hours={history!.busiest_hours} />
          </CardContent>
        </Card>
      )}

      {/* ── Per-zone live board ───────────────────────────────────── */}
      {statusQuery.isError ? (
        // The app is enabled but core cannot reach its /state: say so,
        // with the reason, instead of an empty board that looks like
        // "nothing is happening".
        <ErrorCard
          title="Occupancy app not reachable"
          message={extractApiError(statusQuery.error, 'Core could not fetch the app\'s live state. Is the occupancy-counting container running and registered?')}
          onRetry={() => statusQuery.refetch()}
        />
      ) : statusQuery.isPending && Boolean(occApp) ? (
        <Skeleton className="h-40" />
      ) : zones.length === 0 ? (
        <EmptyState
          icon={<Users size={28} />}
          title="No zones counting yet"
          description={`The app is watching for: ${watchLabels.join(', ')}. It counts every camera assigned the occupancy skill (whole frame until you draw a zone) — if your cameras show vehicles rather than people, add "car" to the watch labels in Configure zones.`}
        />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {zones.map(([key, z]) => {
            const count = Number(z.last_count ?? 0)
            const pct = maxOccupancy > 0
              ? Math.min(100, Math.round((count / maxOccupancy) * 100))
              : 0
            return (
              <Card key={key}>
                <CardContent className="py-4">
                  <div className="flex items-start justify-between">
                    <div>
                      <div className="text-sm font-medium">{cameraName(key)}</div>
                      <div className="text-3xl font-semibold mt-1">
                        {count}
                        {maxOccupancy > 0 && (
                          <span className="text-sm font-normal text-[var(--text-dim)]"> / {maxOccupancy}</span>
                        )}
                      </div>
                    </div>
                    <div className="flex flex-col items-end gap-1">
                      {levelBadge(z.level)}
                      {state?.heatmap_enabled !== false && (
                        <Button
                          variant="ghost"
                          onClick={() => setHeatmapFor(key)}
                          title="Where watched entities stand on this camera — foot-point heatmap"
                        >
                          <Flame size={14} /> Heatmap
                        </Button>
                      )}
                    </div>
                  </div>
                  {maxOccupancy > 0 && (
                    <div className="mt-3 h-1.5 rounded bg-[var(--bg-2)] overflow-hidden">
                      <div
                        className={`h-full rounded ${z.level === 'over'
                          ? 'bg-[var(--danger,#e5484d)]'
                          : pct >= 80 ? 'bg-[var(--warning,#b7791f)]'
                          : 'bg-[var(--success,#46a758)]'}`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  )}
                  <OccupancySparkline
                    samples={seriesFor(key)}
                    ceiling={maxOccupancy}
                  />
                </CardContent>
              </Card>
            )
          })}
        </div>
      )}

      {heatmapFor !== null && (
        <HeatmapDialog
          cameraId={cameraIdOf(heatmapFor)}
          cameraLabel={cameraName(heatmapFor)}
          watchLabels={watchLabels}
          onClose={() => setHeatmapFor(null)}
        />
      )}
    </section>
  )
}

// ── Spatial heatmap ─────────────────────────────────────────────────
// Sequential magnitude → ONE hue, light→dark, painted over a still of
// the camera so the operator reads the scene, not a grid. The grid is
// unit-space (the app bins normalised boxes), so it lines up with any
// still regardless of resolution. Colour carries magnitude only; the
// legend and the stats line carry the numbers.

const HEAT_RANGES: { label: string; hours: number }[] = [
  { label: 'Last hour', hours: 1 },
  { label: 'Today', hours: 24 },
  { label: '7 days', hours: 24 * 7 },
]

/** One hue (the app accent, blue) from light+transparent to dark+opaque. */
function heatColor(t: number): [number, number, number, number] {
  const lo = [147, 197, 253]   // light blue
  const hi = [30, 64, 175]     // deep blue
  const k = Math.max(0, Math.min(1, t))
  return [
    Math.round(lo[0] + (hi[0] - lo[0]) * k),
    Math.round(lo[1] + (hi[1] - lo[1]) * k),
    Math.round(lo[2] + (hi[2] - lo[2]) * k),
    Math.round(255 * (0.18 + 0.72 * k)),
  ]
}

function useCameraStill(cameraId: number) {
  const query = useQuery({
    queryKey: ['camera-snapshot', cameraId],
    queryFn: async () => {
      const { data } = await apiService.getCameraSnapshot(cameraId)
      return URL.createObjectURL(data as Blob)
    },
    retry: 0,
    staleTime: 30_000,
  })
  useEffect(() => {
    const url = query.data
    return () => { if (url) URL.revokeObjectURL(url) }
  }, [query.data])
  return query
}

function HeatmapCanvas({ heat }: { heat: HeatmapResp }) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const canvas = ref.current
    if (!canvas || !heat.cols || !heat.rows) return
    const { cols, rows, cells } = heat
    // Square-root scaling: a hot corner (a queue, a doorway) would
    // otherwise crush every walkway into the lightest step.
    const top = Math.sqrt(Math.max(heat.max, 1))
    const small = document.createElement('canvas')
    small.width = cols
    small.height = rows
    const sctx = small.getContext('2d')!
    const img = sctx.createImageData(cols, rows)
    for (let i = 0; i < cols * rows; i++) {
      const v = cells[i] ?? 0
      if (v <= 0) continue
      const [r, g, b, a] = heatColor(Math.sqrt(v) / top)
      img.data[i * 4] = r
      img.data[i * 4 + 1] = g
      img.data[i * 4 + 2] = b
      img.data[i * 4 + 3] = a
    }
    sctx.putImageData(img, 0, 0)
    // Two smoothed upscales blur cell edges into a continuous field.
    const mid = document.createElement('canvas')
    mid.width = cols * 4
    mid.height = rows * 4
    const mctx = mid.getContext('2d')!
    mctx.imageSmoothingEnabled = true
    mctx.drawImage(small, 0, 0, mid.width, mid.height)
    const ctx = canvas.getContext('2d')!
    canvas.width = cols * 20
    canvas.height = rows * 20
    ctx.imageSmoothingEnabled = true
    ctx.imageSmoothingQuality = 'high'
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(mid, 0, 0, canvas.width, canvas.height)
  }, [heat])
  return (
    <canvas
      ref={ref}
      className="absolute inset-0 h-full w-full pointer-events-none"
      aria-hidden="true"
    />
  )
}

function HeatmapDialog({
  cameraId, cameraLabel, watchLabels, onClose,
}: {
  cameraId: number
  cameraLabel: string
  watchLabels: string[]
  onClose: () => void
}) {
  const [hours, setHours] = useState(24)
  const heatQuery = useQuery({
    queryKey: ['occupancy-heatmap', cameraId, hours],
    queryFn: async () => {
      const { data } = await api.get('/api/v1/occupancy/heatmap', {
        params: { camera_id: cameraId, hours },
      })
      return data as HeatmapResp
    },
    retry: 0,
    refetchInterval: 60_000,
  })
  const still = useCameraStill(cameraId)
  const heat = heatQuery.data
  const hasHeat = !!heat && heat.max > 0
  const perFrame = heat && heat.frames > 0 ? (heat.max / heat.frames) : 0
  return (
    <Modal
      open
      onClose={onClose}
      title={`Heatmap — ${cameraLabel}`}
      widthClassName="w-full max-w-[960px] mx-4"
    >
      <div className="flex flex-wrap items-center gap-2 mb-3">
        {HEAT_RANGES.map((r) => (
          <Button
            key={r.hours}
            variant={r.hours === hours ? 'primary' : 'outline'}
            onClick={() => setHours(r.hours)}
          >
            {r.label}
          </Button>
        ))}
        <span className="text-xs text-[var(--text-dim)] ml-auto">
          Where {watchLabels.join(' / ')} stood (foot point), summed over the window.
        </span>
      </div>

      <div className="relative border border-[var(--border)] bg-black overflow-hidden aspect-[16/9] max-h-[calc(85vh_-_200px)]">
        {still.data ? (
          <img
            src={still.data}
            alt={`current view of ${cameraLabel}`}
            className="absolute inset-0 h-full w-full object-fill opacity-90"
          />
        ) : (
          <div className="absolute inset-0 grid place-items-center text-xs text-[var(--text-dim)]">
            {still.isPending ? 'Fetching a still of the camera…' : 'No camera still available — heat is drawn on a blank frame.'}
          </div>
        )}
        {hasHeat && <HeatmapCanvas heat={heat!} />}
        {heatQuery.isPending && (
          <div className="absolute inset-0 grid place-items-center text-xs text-white/80 bg-black/30">
            Loading heat…
          </div>
        )}
        {!heatQuery.isPending && !hasHeat && (
          <div className="absolute inset-x-0 bottom-0 px-3 py-2 text-xs text-white/85 bg-black/60">
            No heat in this window yet — the app ships its grid about once a minute while it sees {watchLabels.join(' / ')}.
          </div>
        )}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-[var(--text-dim)]"
           style={{ fontVariantNumeric: 'tabular-nums' }}>
        <span className="inline-flex items-center gap-1.5">
          quiet
          <span
            className="inline-block h-2.5 w-28 rounded"
            style={{ background: 'linear-gradient(90deg, rgba(147,197,253,0.25), rgba(30,64,175,0.9))' }}
            aria-hidden="true"
          />
          busy
        </span>
        {heat && hasHeat && (
          <>
            <span>· peak cell {heat.max} hits{perFrame > 0 ? ` (${perFrame.toFixed(2)}/frame)` : ''}</span>
            <span>· {heat.frames} frames over {heat.hours_covered} h</span>
            {heat.updated_at && (
              <span>· updated {new Date(heat.updated_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
            )}
          </>
        )}
        {heatQuery.isError && (
          <span className="text-red-400">{extractApiError(heatQuery.error, 'Could not load the heatmap.')}</span>
        )}
      </div>
    </Modal>
  )
}

// ── Charts ──────────────────────────────────────────────────────────
// Single-series marks styled to the dataviz method: 2px line, soft
// area fill, emphasized endpoint, recessive dashed ceiling gridline,
// text in text tokens (never the series color), tabular numerals,
// native hover titles per sample. One hue (the app accent) — identity
// is carried by the card, so no legend is needed.

/** 24h trend inside a zone card. */
function OccupancySparkline({
  samples,
  ceiling,
}: {
  samples: { t: string; avg: number; max: number }[]
  ceiling: number
}) {
  if (samples.length < 2) {
    return (
      <div className="mt-2 text-[11px] text-[var(--text-dim)]">
        Trend appears as history accrues.
      </div>
    )
  }
  const W = 220, H = 44, PAD = 3
  const t0 = new Date(samples[0].t).getTime()
  const t1 = new Date(samples[samples.length - 1].t).getTime()
  const span = Math.max(1, t1 - t0)
  const top = Math.max(ceiling, ...samples.map((s) => s.max), 1)
  const x = (t: string) => PAD + ((new Date(t).getTime() - t0) / span) * (W - 2 * PAD)
  const y = (v: number) => H - PAD - (v / top) * (H - 2 * PAD)
  const line = samples.map((s, i) => `${i ? 'L' : 'M'}${x(s.t).toFixed(1)},${y(s.avg).toFixed(1)}`).join(' ')
  const area = `${line} L${x(samples[samples.length - 1].t).toFixed(1)},${H - PAD} L${x(samples[0].t).toFixed(1)},${H - PAD} Z`
  const last = samples[samples.length - 1]
  return (
    <div className="mt-2">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: H }} role="img"
           aria-label={`Occupancy over the last 24 hours, currently ${last.avg}`}>
        {ceiling > 0 && (
          <line x1={PAD} x2={W - PAD} y1={y(ceiling)} y2={y(ceiling)}
                stroke="var(--danger,#e5484d)" strokeOpacity="0.35"
                strokeWidth="1" strokeDasharray="3 3" />
        )}
        <path d={area} fill="var(--accent,#3b82f6)" fillOpacity="0.14" />
        <path d={line} fill="none" stroke="var(--accent,#3b82f6)" strokeWidth="2"
              strokeLinejoin="round" strokeLinecap="round" />
        {samples.map((sm) => (
          <circle key={sm.t} cx={x(sm.t)} cy={y(sm.avg)} r="5"
                  fill="transparent">
            <title>{`${new Date(sm.t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · avg ${sm.avg} · peak ${sm.max}`}</title>
          </circle>
        ))}
        <circle cx={x(last.t)} cy={y(last.avg)} r="3.5"
                fill="var(--accent,#3b82f6)" stroke="var(--bg-1,#fff)" strokeWidth="2" />
      </svg>
      <div className="flex justify-between text-[10px] text-[var(--text-dim)]" style={{ fontVariantNumeric: 'tabular-nums' }}>
        <span>24h ago</span>
        <span>avg {last.avg} now</span>
      </div>
    </div>
  )
}

/** Average head-count by hour of day (7d) — one sequential hue, the
 * peak bar direct-labeled, everything else on hover. */
function BusiestHoursChart({ hours }: { hours: { hour: number; avg: number }[] }) {
  const byHour = new Map(hours.map((h) => [h.hour, h.avg]))
  const slots = Array.from({ length: 24 }, (_, h) => ({ hour: h, avg: byHour.get(h) ?? 0 }))
  const top = Math.max(...slots.map((s) => s.avg), 1)
  const peak = slots.reduce((a, b) => (b.avg > a.avg ? b : a))
  const W = 720, H = 96, PAD = 4, LABEL_H = 14
  const bw = (W - 2 * PAD) / 24
  const y = (v: number) => (H - LABEL_H - PAD) * (1 - v / top) + PAD
  const fmtHour = (h: number) => `${String(h).padStart(2, '0')}:00`
  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ minWidth: 480, height: H }}
           role="img" aria-label={`Busiest hour is ${fmtHour(peak.hour)} with an average of ${peak.avg}`}>
        {slots.map((sl) => {
          const barTop = y(sl.avg)
          const barH = Math.max(H - LABEL_H - PAD - barTop, sl.avg > 0 ? 2 : 0)
          const isPeak = sl.hour === peak.hour && sl.avg > 0
          return (
            <g key={sl.hour}>
              {barH > 0 && (
                <rect
                  x={PAD + sl.hour * bw + 1}
                  y={barTop}
                  width={bw - 2}
                  height={barH}
                  rx="3"
                  fill="var(--accent,#3b82f6)"
                  fillOpacity={isPeak ? 1 : 0.55}
                />
              )}
              <rect x={PAD + sl.hour * bw} y={0} width={bw} height={H - LABEL_H}
                    fill="transparent">
                <title>{`${fmtHour(sl.hour)} · avg ${sl.avg}`}</title>
              </rect>
              {isPeak && (
                <text x={PAD + sl.hour * bw + bw / 2} y={Math.max(barTop - 4, 10)}
                      textAnchor="middle" fontSize="10"
                      style={{ fontVariantNumeric: 'tabular-nums' }}
                      fill="var(--text,#1a1a1a)">{sl.avg}</text>
              )}
              {sl.hour % 6 === 0 && (
                <text x={PAD + sl.hour * bw + bw / 2} y={H - 3}
                      textAnchor="middle" fontSize="9"
                      fill="var(--text-dim,#6b7280)">{fmtHour(sl.hour)}</text>
              )}
            </g>
          )
        })}
      </svg>
    </div>
  )
}
