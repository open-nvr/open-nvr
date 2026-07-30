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

export const cameraService = {
  // Streams
  getWhepUrl: (cameraId: number) => api.get(`/api/v1/streams/webrtc/${cameraId}`),
  getHlsUrl: (cameraId: number) => api.get(`/api/v1/streams/hls/${cameraId}`),
  getStreamInfo: (cameraId: number) => api.get(`/api/v1/streams/${cameraId}/info`),
  getStreamUrls: (cameraId: number) => api.get(`/api/v1/streams/${cameraId}/info`), // Alias

  // Camera Management
  getCameras: (params: Record<string, any> = {}) => api.get('/api/v1/cameras/', { params }),
  getCamera: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}`),
  // A single JPEG still of the camera's current view — backs the App
  // Catalog geometry editors (draw a zone/tripwire on the real scene).
  getCameraSnapshot: (cameraId: number) =>
    api.get(`/api/v1/cameras/${cameraId}/snapshot`, { responseType: 'blob' }),
  createCamera: (payload: any) => api.post('/api/v1/cameras/', payload),
  updateCamera: (cameraId: number, payload: any) => api.put(`/api/v1/cameras/${cameraId}`, payload),
  deleteCamera: (cameraId: number) => api.delete(`/api/v1/cameras/${cameraId}`),
  testCameraConnection: (cameraId: number) => api.post(`/api/v1/cameras/${cameraId}/test-connection`),
  
  // Permissions
  assignCameraPermission: (cameraId: number, payload: any) => api.post(`/api/v1/cameras/${cameraId}/permissions`, payload),
  revokeCameraPermission: (cameraId: number, userId: number) => api.delete(`/api/v1/cameras/${cameraId}/permissions/${userId}`),
  checkCameraPermission: (cameraId: number, requireManage = false) => api.get(`/api/v1/cameras/${cameraId}/permissions/check`, { params: { require_manage: requireManage } }),

  // Configuration
  createCameraConfig: (payload: any) => api.post('/api/v1/camera-config/', payload),
  updateCameraConfig: (cameraId: number, payload: any) => api.put(`/api/v1/camera-config/${cameraId}`, payload),
  provisionCameraPath: (cameraId: number) => api.post(`/api/v1/camera-config/${cameraId}/provision`),
  unprovisionCameraPath: (cameraId: number) => api.post(`/api/v1/camera-config/${cameraId}/unprovision`),
  getCameraPathStatus: (cameraId: number) => api.get(`/api/v1/camera-config/${cameraId}/status`),
  getCameraConfig: (cameraId: number) => api.get(`/api/v1/camera-config/${cameraId}`),
  
  // PTZ
  ptzMove: (cameraId: number, x: number, y: number, z: number = 0) =>
    api.post(`/api/v1/cameras/${cameraId}/ptz/move`, null, { params: { x, y, z } }),
  ptzStop: (cameraId: number) =>
    api.post(`/api/v1/cameras/${cameraId}/ptz/stop`),

  // MediaMTX Integration
  provisionCameraMediaMTX: (cameraId: number, params: { enable_recording?: boolean; rtsp_transport?: string; recording_segment_seconds?: number; recording_path?: string } = {}) =>
    api.post(`/api/v1/cameras/${cameraId}/provision-mediamtx`, {}, { params }),
  toggleCameraRecording: (cameraId: number, enable: boolean) => api.post(`/api/v1/cameras/${cameraId}/toggle-recording`, '', { params: { enable } }),
  getCameraMediaMTXStatus: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/mediamtx-status`),

  // ONVIF
  onvifDiscover: (params?: { cidr?: string | string[] }) => api.get('/api/v1/discover', { params }),
  onvifConnect: (
    ip: string,
    params: { username: string; password: string; port?: number }
  ) => api.post(`/api/v1/connect`, '', { params: { ip, ...params } }),
  onvifProfiles: (
    ip: string,
    params: { username: string; password: string; port?: number }
  ) => api.get(`/api/v1/camera/${encodeURIComponent(ip)}/profiles`, { params }),
  onvifStreamUri: (
    ip: string,
    params: { username: string; password: string; profileToken: string; port?: number }
  ) => api.get(`/api/v1/camera/${encodeURIComponent(ip)}/stream-uri`, { params }),
  onvifPtzMove: (
    ip: string,
    params: { username: string; password: string; profileToken: string; x?: number; y?: number; z?: number; port?: number }
  ) => api.post(`/api/v1/camera/${encodeURIComponent(ip)}/ptz/move`, '', { params }),
  onvifPtzStop: (
    ip: string,
    params: { username: string; password: string; profileToken: string; port?: number }
  ) => api.post(`/api/v1/camera/${encodeURIComponent(ip)}/ptz/stop`, '', { params }),
  onvifPreset: (
    ip: string,
    params: { username: string; password: string; profileToken: string; action: 'setPreset' | 'getPresets' | 'gotoPreset'; name?: string; presetToken?: string; port?: number }
  ) => api.post(`/api/v1/camera/${encodeURIComponent(ip)}/ptz/preset`, '', { params }),
  onvifGetTime: (
    ip: string,
    params: { port?: number }
  ) => api.get(`/api/v1/camera/${encodeURIComponent(ip)}/time`, { params }),
  onvifSyncTime: (
    ip: string,
    params: { username: string; password: string; port?: number }
  ) => api.post(`/api/v1/camera/${encodeURIComponent(ip)}/time/sync`, '', { params }),

  // Camera Settings (device driver — Phase 0: read-only)
  getCameraCapabilities: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/capabilities`),
  refreshCameraCapabilities: (cameraId: number) => api.post(`/api/v1/cameras/${cameraId}/capabilities/refresh`),
  getCameraDeviceInfo: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/info`),
  getCameraNetwork: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/network`),
  getCameraStorage: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/storage`),
  getCameraTime: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/time`),
  syncCameraTimeManual: (cameraId: number, payload: { timezone?: string | null }) =>
    api.post(`/api/v1/cameras/${cameraId}/time/sync-manual`, payload),
  setCameraNtp: (cameraId: number, payload: { server: string; timezone?: string | null }) =>
    api.put(`/api/v1/cameras/${cameraId}/time/ntp`, payload),
  getCameraImaging: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/imaging`),
  setCameraImaging: (cameraId: number, payload: Record<string, any>) =>
    api.put(`/api/v1/cameras/${cameraId}/imaging`, payload),
  getCameraEncoder: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/encoder`),
  setCameraEncoder: (cameraId: number, configToken: string, payload: Record<string, any>) =>
    api.put(`/api/v1/cameras/${cameraId}/encoder/${encodeURIComponent(configToken)}`, payload),
  getCameraOsd: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/osd`),
  setCameraOsd: (cameraId: number, payload: Record<string, any>) =>
    api.put(`/api/v1/cameras/${cameraId}/osd`, payload),
  getCameraMotion: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/motion`),
  setCameraMotion: (cameraId: number, payload: Record<string, any>) =>
    api.put(`/api/v1/cameras/${cameraId}/motion`, payload),
  getCameraServices: (cameraId: number) =>
    api.get(`/api/v1/cameras/${cameraId}/services`),
  setCameraService: (cameraId: number, key: string, enabled: boolean) =>
    api.put(`/api/v1/cameras/${cameraId}/services/${key}`, { enabled }),
  exportCameraConfig: (cameraId: number, secretKey: string) =>
    api.post(
      `/api/v1/cameras/${cameraId}/config-backup`,
      { secret_key: secretKey },
      { responseType: 'blob' }
    ),
  getCameraSecurity: (cameraId: number) =>
    api.get(`/api/v1/cameras/${cameraId}/security`),
  setCameraIpFilter: (cameraId: number, payload: Record<string, any>) =>
    api.put(`/api/v1/cameras/${cameraId}/security/ip-filter`, payload),
  setCameraCloud: (cameraId: number, enabled: boolean) =>
    api.put(`/api/v1/cameras/${cameraId}/security/cloud`, { enabled }),
  getCameraSmart: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/smart`),
  setCameraSmart: (cameraId: number, key: string, payload: Record<string, any>) =>
    api.put(`/api/v1/cameras/${cameraId}/smart/${key}`, payload),

  // Camera events (native alarm stream)
  getCameraEvents: (cameraId: number, limit = 50) =>
    api.get(`/api/v1/cameras/${cameraId}/events`, { params: { limit } }),
  getCameraEventsStatus: (cameraId: number) =>
    api.get(`/api/v1/cameras/${cameraId}/events/status`),
  subscribeCameraEvents: (cameraId: number) =>
    api.post(`/api/v1/cameras/${cameraId}/events/subscribe`),
  unsubscribeCameraEvents: (cameraId: number) =>
    api.post(`/api/v1/cameras/${cameraId}/events/unsubscribe`),

  // Per-camera AI detectors (reuse ai-model-management)
  getAiModels: () => api.get('/api/v1/ai-model-management'),
  startInference: (modelId: number) =>
    api.post(`/api/v1/ai-model-management/${modelId}/start-inference`),
  stopInference: (modelId: number) =>
    api.post(`/api/v1/ai-model-management/${modelId}/stop-inference`),
  getInferenceStatus: (modelId: number) =>
    api.get(`/api/v1/ai-model-management/${modelId}/inference-status`),

  // On-camera accounts + reboot (gated)
  getCameraUsers: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/camera-users`),
  createCameraUser: (cameraId: number, payload: { username: string; password: string; level: string }) =>
    api.post(`/api/v1/cameras/${cameraId}/camera-users`, payload),
  deleteCameraUser: (cameraId: number, username: string) =>
    api.delete(`/api/v1/cameras/${cameraId}/camera-users/${encodeURIComponent(username)}`),
  rebootCamera: (cameraId: number) => api.post(`/api/v1/cameras/${cameraId}/reboot`),
}
