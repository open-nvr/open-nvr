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

import { api } from '../lib/api'
import { browserTz, localDayEnd, localDayStart } from '../lib/time'

export const recordingService = {
  // Playback
  // Note: these go through the shared api client, which already sends the
  // bearer token in the Authorization header. We deliberately do NOT pass the
  // JWT as a ?token= query param (it would leak into access logs/history).
  getRecordingsByDate: (cameraId?: number) => {
    // tz: day buckets are the BROWSER'S local days (midnight -> midnight).
    const params: Record<string, any> = { tz: browserTz() }
    if (cameraId) params.camera_id = cameraId
    return api.get('/api/v1/recordings/list', { params })
  },
  getRecordings: (opts?: { limit?: number; offset?: number; camera_id?: number }) => {
    return api.get('/api/v1/recordings/playback/cameras', { params: opts })
  },
  getRecordingSessionsForAI: (params?: { camera_id?: number }) => {
    return api.get('/api/v1/recordings/sessions-for-ai', { params })
  },
  getRecordingStats: () => api.get('/api/v1/recordings/stats'),
  getPlaybackConfig: () => api.get('/api/v1/recordings/config'),
  getPlaybackList: (path: string) => {
    return api.get('/api/v1/recordings/playback/list', { params: { path } })
  },
  getPlaybackUrl: (path: string, start: string, duration: number) => {
    return api.get('/api/v1/recordings/playback/url', { params: { path, start, duration } })
  },
  // Continuous recording segments for a camera on a given LOCAL day
  // (YYYY-MM-DD). Powers the DVR playback timeline (footage/gap blocks +
  // wall-clock seeking). The browser computes local midnight -> next local
  // midnight explicitly so the server never has to guess the day boundary.
  getSegments: (cameraId: number, date?: string) => {
    const params: Record<string, any> = { tz: browserTz() }
    if (date) {
      params.date = date
      params.start = new Date(localDayStart(date)).toISOString()
      params.end = new Date(localDayEnd(date)).toISOString()
    }
    return api.get(`/api/v1/recordings/segments/${cameraId}`, { params })
  },

  // HLS VOD
  createHlsPlaybackSession: (params: { camera_id: number; start: string; end: string }) => {
    return api.get('/api/v1/recordings/playback/hls', { params })
  },
  deleteHlsPlaybackSession: (sessionId: string) => {
    return api.delete(`/api/v1/recordings/playback/hls/${sessionId}`)
  },

  // Clip export: mint a short-lived single-use ticket (authenticated), then
  // navigate an <a> at the returned download_url — the backend streams the
  // clip to disk, so nothing is buffered in browser memory.
  createClipExportTicket: (params: {
    camera_id: number
    start: string
    duration: number
    filename?: string
  }) => api.post('/api/v1/recordings/export/ticket', undefined, { params }),

  // Cloud upload
  queueCloudUploadForDay: (cameraId: number, date: string) => {
    return api.post('/api/v1/recordings/cloud-upload/day', undefined, {
      params: { camera_id: cameraId, date },
    })
  },
  getCloudUploadStatus: () => api.get('/api/v1/recordings/cloud-upload/status'),

  // Storage & Retention
  getRecordingStorage: () => api.get('/api/v1/recordings/storage'),
  updateRecordingStorage: (payload: any) => api.put('/api/v1/recordings/storage', payload),
  getRecordingRetention: () => api.get('/api/v1/recordings/retention'),
  updateRecordingRetention: (payload: any) => api.put('/api/v1/recordings/retention', payload),

  // Orphaned (quarantined) recording trees — footage that could not be
  // positively attributed to a current camera after a DB rebuild. Superuser.
  getRecordingOrphans: () => api.get('/api/v1/recordings/orphans'),
  attachRecordingOrphan: (orphanId: string, cameraId: number) =>
    api.post(`/api/v1/recordings/orphans/${encodeURIComponent(orphanId)}/attach`, {
      camera_id: cameraId,
    }),
  deleteRecordingOrphan: (orphanId: string) =>
    api.delete(`/api/v1/recordings/orphans/${encodeURIComponent(orphanId)}`),
  rescanRecordingOrphans: () => api.post('/api/v1/recordings/orphans/rescan'),
}
