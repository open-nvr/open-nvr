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

import { QueryClient, useQueries, useQuery } from '@tanstack/react-query'
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
  status?: string | null
}

export type CameraListResp = { cameras: CameraItem[]; total: number }

export function useCameras(params: { limit?: number; active_only?: boolean } = { limit: 100, active_only: true }) {
  return useQuery({
    queryKey: ['cameras', params],
    queryFn: async () => {
      const { data } = await apiService.getCameras(params)
      return data as CameraListResp
    },
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
          recordings?: { date: string; total_duration?: number }[]
        }[]
        total_recordings?: number
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

export function useSuricataStats(limit = 5000) {
  return useQuery({
    queryKey: ['suricata-stats', limit],
    queryFn: async () => {
      const { data } = await apiService.getSuricataStats({ limit })
      return data as { by_severity?: Record<string, number> }
    },
  })
}
