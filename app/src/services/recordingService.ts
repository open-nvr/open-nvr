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

export const recordingService = {
  // Playback
  getRecordingsByDate: (cameraId?: number, token?: string) => {
    const params: Record<string, any> = {}
    if (cameraId) params.camera_id = cameraId
    if (token) params.token = token
    return api.get('/api/v1/recordings/list', { params })
  },
  getRecordings: (opts?: { limit?: number; offset?: number; camera_id?: number }) => {
    return api.get('/api/v1/recordings/playback/cameras', { params: opts })
  },
  getRecordingSessionsForAI: (params?: { camera_id?: number }) => {
    return api.get('/api/v1/recordings/sessions-for-ai', { params })
  },
  getRecordingStats: (token?: string) => {
    const params: Record<string, any> = {}
    if (token) params.token = token
    return api.get('/api/v1/recordings/stats', { params })
  },
  getPlaybackConfig: () => api.get('/api/v1/recordings/config'),
  getPlaybackList: (path: string, token?: string) => {
    const params: Record<string, any> = { path }
    if (token) params.token = token
    return api.get('/api/v1/recordings/playback/list', { params })
  },
  getPlaybackUrl: (path: string, start: string, duration: number, token?: string) => {
    const params: Record<string, any> = { path, start, duration }
    if (token) params.token = token
    return api.get('/api/v1/recordings/playback/url', { params })
  },
  getTodaySegments: (cameraId: number) => api.get(`/api/v1/recordings/today/${cameraId}`),

  // HLS VOD
  createHlsPlaybackSession: (params: { camera_id: number; start: string; end: string }) => {
    return api.get('/api/v1/recordings/playback/hls', { params })
  },
  deleteHlsPlaybackSession: (sessionId: string) => {
    return api.delete(`/api/v1/recordings/playback/hls/${sessionId}`)
  },

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
}
