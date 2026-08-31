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

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Settings2, Users } from 'lucide-react'
import { api } from '../lib/api'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { useSnackbar } from '../components/Snackbar'
import {
  Badge, Button, Card, CardContent,
  EmptyState, PageHeader, Skeleton,
} from '../components/ui'
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
  cameras?: Record<string, OccupancyCameraState>
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
  const cameraName = (key: string) => {
    const m = /^cam(\d+)$/.exec(key)
    const id = m ? Number(m[1]) : Number(key)
    return camerasQuery.data?.find((c) => c.id === id)?.name ?? key
  }

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
      {statusQuery.isPending && Boolean(occApp) ? (
        <Skeleton className="h-40" />
      ) : zones.length === 0 ? (
        <EmptyState
          icon={<Users size={28} />}
          title="No zones counting yet"
          description="Draw a zone on a camera in the app's Configure form (Configure zones above) and its live head-count appears here within seconds."
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
                    {levelBadge(z.level)}
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
    </section>
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
