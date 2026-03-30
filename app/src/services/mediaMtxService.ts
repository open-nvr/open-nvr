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

export const mediaMtxService = {
  getMediaSourceSettings: () => api.get('/api/v1/media-source/settings'),
  updateMediaSourceSettings: (payload: any) => api.put('/api/v1/media-source/settings', payload),
  uploadMediaSourceSettings: (files: FormData | { cert_file?: File; key_file?: File; ca_bundle_file?: File }) => {
    if (files instanceof FormData) {
      return api.post('/api/v1/media-source/settings/upload', files)
    }
    const form = new FormData()
    if (files.cert_file) form.append('cert_file', files.cert_file)
    if (files.key_file) form.append('key_file', files.key_file)
    if (files.ca_bundle_file) form.append('ca_bundle_file', files.ca_bundle_file)
    return api.post('/api/v1/media-source/settings/upload', form)
  },

  // Admin
  mtxGlobalGet: () => api.get('/api/v1/mediamtx/admin/global'),
  mtxGlobalPatch: (payload: any) => api.patch('/api/v1/mediamtx/admin/global', payload as any),
  mtxPathdefaultsGet: () => api.get('/api/v1/mediamtx/admin/pathdefaults'),
  mtxPathdefaultsPatch: (payload: any) => api.patch('/api/v1/mediamtx/admin/pathdefaults', payload as any),
  mtxPathsList: () => api.get('/api/v1/mediamtx/admin/paths/list'),
  mtxPathGet: (cameraId: number) => api.get(`/api/v1/mediamtx/admin/paths/${cameraId}`),
  mtxPathPatch: (cameraId: number, payload: any) => api.patch(`/api/v1/mediamtx/admin/paths/${cameraId}`, payload as any),
  mtxPushRtsp: (cameraId: number, rtspUrl: string, enableRecording: boolean) =>
    api.post(`/api/v1/mediamtx/admin/streams/push/${cameraId}`, '', { params: { rtsp_url: rtspUrl, enable_recording: enableRecording } }),
  mtxEnableRecording: (cameraId: number, duration: string = '60s', segmentDuration: string = '10s') =>
    api.post(`/api/v1/mediamtx/admin/recordings/enable/${cameraId}`, '', { params: { duration, segment_duration: segmentDuration } }),
  mtxDisableRecording: (cameraId: number) =>
    api.post(`/api/v1/mediamtx/admin/recordings/disable/${cameraId}`, ''),
  
  // Health
  mtxHealth: () => api.get('/api/v1/mediamtx/health'),
}
