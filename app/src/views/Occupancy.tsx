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
                </CardContent>
              </Card>
            )
          })}
        </div>
      )}
    </section>
  )
}
