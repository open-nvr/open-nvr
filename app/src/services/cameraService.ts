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

/** True when a create failed with the backend's duplicate-camera 409; the
 * caller should offer "add anyway" and retry with { force: true }. */
export function isDuplicateCameraError(e: any): boolean {
  return e?.status === 409 && e?.data?.detail?.code === 'duplicate_camera'
}

/** Names of the already-added cameras carried in a duplicate-camera 409. */
export function duplicateCameraNames(e: any): string[] {
  const dups = e?.data?.detail?.duplicates
  return Array.isArray(dups) ? dups.map((d: any) => d?.name).filter(Boolean) : []
}

export type CameraMaskKind = 'motion' | 'object'
export type CameraMaskBody = {
  name: string
  kind: CameraMaskKind
  polygon: number[][]
  labels: string[] | null
}

export type ObjectFilter = {
  min_area?: number | null
  max_area?: number | null
  min_ratio?: number | null
  max_ratio?: number | null
  min_score?: number | null
}
export type DetectionSettings = {
  fps?: number | null
  motion_enabled?: boolean | null
  motion_threshold?: number | null
  motion_contour_area?: number | null
  min_spawn_score?: number | null
  stationary_interval?: number | null
  /** Which stream this camera detects on: auto (sub when tall enough, else main), sub, main. */
  detect_stream?: 'auto' | 'sub' | 'main' | null
  object_filters?: Record<string, ObjectFilter> | null
}

/** The escalation proposal for a camera (services/evidence_escalation.py):
 *  evidence quality over the last day and the decision in force. */
export type EscalationView = {
  camera_id: number
  escalation: {
    status: 'proposed' | 'accepted' | 'dismissed'
    reasons?: string[]
    proposed_at?: string | null
    accepted_at?: string | null
    dismissed_until?: string | null
    previous?: string | null
    by?: string | null
    review_due?: boolean
    cost_note?: string
  } | null
  evaluation: {
    window_hours: number
    visits: number
    full_res: number
    small: number
    full_res_share: number | null
    small_share: number | null
    detect_stream: string
    qualifies: boolean
    reasons: string[]
    thresholds: { min_visits: number; full_res_min: number; small_px: number; small_share_max: number }
    cost_note: string
  } | null
}

export const cameraService = {
  // Streams
  getWhepUrl: (cameraId: number) => api.get(`/api/v1/streams/webrtc/${cameraId}`),
  getHlsUrl: (cameraId: number) => api.get(`/api/v1/streams/hls/${cameraId}`),
  getStreamInfo: (cameraId: number) => api.get(`/api/v1/streams/${cameraId}/info`),
  getStreamUrls: (cameraId: number) => api.get(`/api/v1/streams/${cameraId}/info`), // Alias

  // Webcam cameras (source_type "publisher"): the browser publisher page
  // asks for a short-lived WHIP session on every (re)connect.
  // -> { whip_url, token, stream_name, expires_in_seconds }
  getWhipSession: (cameraId: number) => api.post(`/api/v1/streams/${cameraId}/whip`),
  // The host agent on the OpenNVR machine (USB webcams, host metrics):
  // -> HostAgentInfo (lib/hostAgent.ts)
  getHostAgent: () => api.get('/api/v1/host-agent'),
  // USB webcams MediaMTX serves itself (Linux, WEBCAMS=on): available=false elsewhere.
  getLocalWebcams: () => api.get('/api/v1/webcams/local'),

  // Camera Management
  // With a `limit`, one page, for callers that page themselves (the Cameras
  // table). Without one, EVERY camera: the endpoint pages at 100 by default,
  // and pickers, the dashboard and Live View used to stop there (#671).
  getCameras: async (params: Record<string, any> = {}) => {
    if (params.limit != null) return api.get('/api/v1/cameras/', { params })
    const pageSize = 500
    const cameras: any[] = []
    for (;;) {
      const res = await api.get('/api/v1/cameras/', {
        params: { ...params, skip: cameras.length, limit: pageSize },
      })
      const rows = res.data?.cameras ?? []
      cameras.push(...rows)
      if (rows.length < pageSize || cameras.length >= (res.data?.total ?? 0)) {
        return { ...res, data: { ...res.data, cameras, total: res.data?.total ?? cameras.length } }
      }
    }
  },
  // Named areas of a camera's picture (HA-109).
  listCameraZones: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/zones`),
  createCameraZone: (cameraId: number, body: { name: string; polygon: number[][]; labels: string[] | null }) =>
    api.post(`/api/v1/cameras/${cameraId}/zones`, body),
  updateCameraZone: (cameraId: number, zoneId: number, body: { name: string; polygon: number[][]; labels: string[] | null }) =>
    api.put(`/api/v1/cameras/${cameraId}/zones/${zoneId}`, body),
  deleteCameraZone: (cameraId: number, zoneId: number) =>
    api.delete(`/api/v1/cameras/${cameraId}/zones/${zoneId}`),
  // What Tier-0 must ignore in a camera's picture: motion masks (blind the
  // motion gate) and object masks (drop detections standing inside).
  listCameraMasks: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/masks`),
  createCameraMask: (cameraId: number, body: CameraMaskBody) =>
    api.post(`/api/v1/cameras/${cameraId}/masks`, body),
  updateCameraMask: (cameraId: number, maskId: number, body: CameraMaskBody) =>
    api.put(`/api/v1/cameras/${cameraId}/masks/${maskId}`, body),
  deleteCameraMask: (cameraId: number, maskId: number) =>
    api.delete(`/api/v1/cameras/${cameraId}/masks/${maskId}`),
  // This camera's overrides of the detect-pipeline's global knobs. PUT
  // replaces the whole record; {} clears every override.
  getDetectionSettings: (cameraId: number) =>
    api.get(`/api/v1/cameras/${cameraId}/detection-settings`),
  putDetectionSettings: (cameraId: number, body: DetectionSettings) =>
    api.put(`/api/v1/cameras/${cameraId}/detection-settings`, body),
  // The escalation proposal: weak evidence on the sub stream → detect on main?
  getEscalation: (cameraId: number, fresh = false) =>
    api.get(`/api/v1/cameras/${cameraId}/escalation`, { params: fresh ? { fresh: true } : {} }),
  acceptEscalation: (cameraId: number) => api.post(`/api/v1/cameras/${cameraId}/escalation/accept`),
  dismissEscalation: (cameraId: number, days = 7) =>
    api.post(`/api/v1/cameras/${cameraId}/escalation/dismiss`, { days }),
  resetEscalation: (cameraId: number) => api.post(`/api/v1/cameras/${cameraId}/escalation/reset`),
  getCamera: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}`),
  // A single JPEG still of the camera's current view — backs the App
  // Catalog geometry editors (draw a zone/tripwire on the real scene).
  getCameraSnapshot: (cameraId: number) =>
    api.get(`/api/v1/cameras/${cameraId}/snapshot`, { responseType: 'blob' }),
  createCamera: (payload: any, opts: { force?: boolean } = {}) =>
    api.post('/api/v1/cameras/', payload, opts.force ? { params: { force: true } } : undefined),
  updateCamera: (cameraId: number, payload: any) => api.put(`/api/v1/cameras/${cameraId}`, payload),
  // Suggestions + live availability for the camera Assignments editor.
  // Claims: which cameras a skill is pointed at. An app's camera pick is
  // one of these (consumer `app:<id>`), and a pick is what turns the app
  // ON for a camera — so these need the same permission as editing the
  // camera; the server enforces that.
  // Read-only: the apps that picked this camera.
  getCameraUsedBy: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/used-by`),
  getSkillCameras: (skill: string) =>
    api.get(`/api/v1/skills/${encodeURIComponent(skill)}/cameras`),
  declareSkillCamera: (skill: string, cameraId: number, consumer: string) =>
    api.put(`/api/v1/skills/${encodeURIComponent(skill)}/cameras/${cameraId}`, { consumer }),
  releaseSkillCamera: (skill: string, cameraId: number, consumer: string) =>
    api.delete(`/api/v1/skills/${encodeURIComponent(skill)}/cameras/${cameraId}`, {
      params: { consumer },
    }),
  deleteCamera: (cameraId: number) => api.delete(`/api/v1/cameras/${cameraId}`),
  // Bin (irreversibly soft-deleted cameras)
  getDeletedCameras: () => api.get('/api/v1/cameras/deleted'),
  // The locations among the cameras the caller can see, with counts, for the
  // Cameras page's Location filter; `none` counts cameras without one.
  getCameraLocations: (params: { active_only?: boolean } = {}) =>
    api.get('/api/v1/cameras/locations', { params }),
  // Import and export: a spreadsheet of cameras (CSV or Excel) in and out.
  exportCameras: (params: Record<string, unknown>, mfaCode?: string) =>
    api.get('/api/v1/cameras/export', {
      params,
      responseType: 'blob',
      ...(mfaCode ? { headers: { 'X-MFA-Code': mfaCode } } : {}),
    }),
  getCameraImportTemplate: (format: 'csv' | 'xlsx') =>
    api.get('/api/v1/cameras/import/template', { params: { format }, responseType: 'blob' }),
  previewCameraImport: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api.post('/api/v1/cameras/import/preview', form)
  },
  startCameraImport: (file: File, mode: 'add' | 'add_update', probe: boolean) => {
    const form = new FormData()
    form.append('file', file)
    form.append('mode', mode)
    form.append('probe', String(probe))
    return api.post('/api/v1/cameras/import', form)
  },
  getCameraImportJob: (jobId: string) => api.get(`/api/v1/cameras/import/jobs/${jobId}`),
  hardDeleteCamera: (cameraId: number, confirmationPhrase: string, mfaCode: string) =>
    api.post(
      `/api/v1/cameras/${cameraId}/hard-delete`,
      { confirmation_phrase: confirmationPhrase },
      { headers: { 'X-MFA-Code': mfaCode } }
    ),
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
  onvifDiscover: (params?: { cidr?: string | string[] }, signal?: AbortSignal) =>
    api.get('/api/v1/discover', { params, signal }),
  onvifDiscoverPlan: () => api.get('/api/v1/discover/plan'),
  // ONVIF cameras found by the host agent's WS-Discovery (multicast) on the
  // OpenNVR machine's own network: -> { available, devices, scanned_at }
  onvifDiscoverAgent: (signal?: AbortSignal) => api.get('/api/v1/discover/agent', { signal }),
  onvifConnect: (
    ip: string,
    params: { username: string; password: string; port?: number }
  ) => api.post(`/api/v1/connect`, '', { params: { ip, ...params } }),

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
  getAnalyticsSource: (cameraId: number) => api.get(`/api/v1/cameras/${cameraId}/analytics-source`),
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
