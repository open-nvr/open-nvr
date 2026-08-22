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

// Shared react-query hooks. Views that need the same server data must use
// these instead of calling apiService in a useEffect, so concurrent mounts
// share one request and one cache entry.

import { QueryClient, keepPreviousData, useQueries, useQuery } from '@tanstack/react-query'
import { apiService } from './apiService'
import { todayLocalKey } from './time'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

export type CameraItem = {
  id: number
  name: string
  ip_address: string
  is_active: boolean
  /** Provisioning lifecycle ('provisioned' | 'active' | 'error' | ...), NOT liveness. */
  status?: string | null
  mediamtx_provisioned?: boolean | null
  /**
   * Live connectivity from the backend's in-memory tracker.
   * `null`/absent means UNKNOWN, not offline — the recorder restarted and has
   * not re-seeded yet, or the camera is paused. Render it distinctly.
   */
  live_online?: boolean | null
  /** Observed recording health: 'recording' | 'stalled' | 'never' | 'off'. */
  recording_state?: string | null
  last_recording_at?: string | null
}

export type CameraListResp = { cameras: CameraItem[]; total: number }

export type CameraListParams = {
  skip?: number
  limit?: number
  active_only?: boolean
  q?: string
}

/**
 * The camera list, and the only place the UI learns a camera's live state.
 *
 * Kept fresh two ways. The events socket invalidates this key on every
 * `camera_status` transition (see CameraStatusProvider), which covers
 * connects and disconnects within a second or so. The interval below is the
 * floor under that, and it is not optional:
 *
 * * `recording_state` is TIME-derived — a camera silently crosses the
 *   watchdog's stall threshold with no event on the bus, so push alone can
 *   never keep that badge honest;
 * * a camera already streaming when the backend restarted seeds silently
 *   (handle_signal only emits when the previous state was False), so no
 *   transition is ever published for it.
 */
export function useCameras(
  params: CameraListParams = { limit: 100, active_only: true },
) {
  return useQuery({
    queryKey: ['cameras', params],
    queryFn: async () => {
      const { data } = await apiService.getCameras(params)
      return data as CameraListResp
    },
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    // Paginating or typing in the search box swaps the query key; without
    // this the table would blank out and remount on every keystroke pause.
    placeholderData: keepPreviousData,
  })
}

/**
 * Media-server reachability, shared so every caller resolves to one request.
 * When this is down every camera's stream badge is meaningless, so the UI
 * says so once rather than reporting each camera as individually broken.
 */
export function useMediaMtxHealth() {
  return useQuery({
    queryKey: ['mtx-health'],
    queryFn: async () => {
      const { data } = await apiService.mtxHealth()
      return data as { status?: string }
    },
    refetchInterval: 30_000,
    staleTime: 10_000,
  })
}

export function useRecordingsByDate() {
  return useQuery({
    queryKey: ['recordings-by-date'],
    queryFn: async () => {
      const { data } = await apiService.getRecordingsByDate()
      return data as {
        cameras?: {
          camera_id?: number
          camera_name?: string
          /** Number of local days with footage for this camera. */
          recording_count?: number
          /** This camera's total footage duration in seconds. */
          total_duration?: number
          recordings?: { date: string; total_duration?: number; segment_count?: number }[]
        }[]
        /** Camera-day count (1 per camera per local day) — not a file count. */
        total_recordings?: number
        /** Total footage duration in seconds across all cameras. */
        total_duration?: number
        mediamtx_available?: boolean
      }
    },
  })
}

export type SegmentsResponse = {
  segments?: { start: string; duration: number; playback_url?: string }[]
  live_edge_start?: string | null
  camera_id?: number
}

/**
 * Per-camera timeline segments for one LOCAL day.
 *
 * The RAW response is what's cached: react-query's structural sharing keeps
 * it referentially stable when the server returns identical data, so a poll
 * tick that changes nothing re-renders nothing (this is what killed the old
 * "fresh objects every 15s invalidate the world" cascade). Parse to
 * timeline shapes with useMemo on the consumer side.
 */
export function useCameraSegments(
  cameraId: number,
  date: string | null,
  opts: { poll?: boolean; pollMs?: number } = {}
) {
  const polling = !!opts.poll && date === todayLocalKey()
  return useQuery({
    queryKey: ['segments', cameraId, date],
    enabled: cameraId > 0 && !!date,
    queryFn: async () => {
      const { data } = await apiService.getSegments(cameraId, date || undefined)
      return data as SegmentsResponse
    },
    // Only today's timeline grows; historical days never change.
    refetchInterval: polling ? opts.pollMs ?? 15_000 : false,
    staleTime: polling ? 5_000 : 60_000,
  })
}

/** Multi-camera variant for the sync playback grid (one query per camera). */
export function useSegmentsForCameras(
  cameraIds: number[],
  date: string | null,
  opts: { poll?: boolean; pollMs?: number } = {}
) {
  const polling = !!opts.poll && date === todayLocalKey()
  return useQueries({
    queries: cameraIds.map((id) => ({
      queryKey: ['segments', id, date],
      enabled: id > 0 && !!date,
      queryFn: async () => {
        const { data } = await apiService.getSegments(id, date || undefined)
        return data as SegmentsResponse
      },
      refetchInterval: polling ? opts.pollMs ?? 15_000 : (false as const),
      staleTime: polling ? 5_000 : 60_000,
    })),
  })
}

export type SystemResourcesResp = {
  sampled_at?: string
  scope?: string
  monitoring_available?: boolean
  cpu_percent?: number | null
  load_avg?: number[] | null
  memory?: { total: number; used: number; percent: number } | null
  disk?: { path: string; total: number; used: number; free: number; percent: number } | null
  disk_error?: string | null
  active_alerts?: { alert_type: string; severity: string; description: string; since?: string }[]
  thresholds?: {
    enabled?: boolean
    cpu_percent_threshold?: number | null
    memory_percent_threshold?: number | null
    disk_min_free_gb?: number | null
    disk_used_percent_threshold?: number | null
  }
}

/** Host CPU/RAM/disk snapshot — shared by the Dashboard health card, the
 * disk-critical banner, and RecordingSettings (one cache entry, one poll). */
export function useSystemResources() {
  return useQuery({
    queryKey: ['system-resources'],
    queryFn: async () => {
      const { data } = await apiService.getSystemResources()
      return data as SystemResourcesResp
    },
    refetchInterval: 15_000,
    staleTime: 5_000,
  })
}

export function useSuricataStats(limit = 5000) {
  return useQuery({
    queryKey: ['suricata-stats', limit],
    queryFn: async () => {
      const { data } = await apiService.getSuricataStats({ limit })
      return data as { by_severity?: Record<string, number> }
    },
  })
}
