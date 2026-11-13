// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { api } from '../lib/api'
import type { Slot, TourPageSpec } from '../lib/tourPlan'

export type AlertSource = 'app_alert' | 'camera_event'
export type AlertSeverity = 'low' | 'medium' | 'high' | 'critical'

/** Alarm takeover (routers/tours.py TourAlerts). */
export interface TourAlertSettings {
  enabled: boolean
  sources: AlertSource[]
  /** App alerts below this are ignored; camera alarms carry no severity. */
  min_severity: AlertSeverity
  /** 'tour' = only this tour's cameras; 'visible' = any camera the viewer sees. */
  scope: 'tour' | 'visible'
  hold_s: number
  cooldown_s: number
}

export interface TourSettings {
  fit: 'fit' | 'fill'
  /** How one page gives way to the next. */
  transition: 'cut' | 'fade'
  /** Play the next page hidden for its last seconds, so it starts at once. */
  preload: boolean
  alerts: TourAlertSettings
}

export const DEFAULT_TOUR_SETTINGS: TourSettings = {
  fit: 'fit',
  transition: 'fade',
  preload: true,
  alerts: { enabled: false, sources: ['app_alert', 'camera_event'], min_severity: 'medium', scope: 'tour', hold_s: 30, cooldown_s: 60 },
}

/** A tour as the server returns it: every page already filtered to the viewer. */
export interface Tour {
  id: number
  name: string
  description?: string | null
  enabled: boolean
  revision: number
  settings?: TourSettings
  /** False unless they may manage every camera in it: they may run it, not change it. */
  editable: boolean
  pages: TourPageSpec[]
  created_at?: string | null
  updated_at?: string | null
}

export interface TourInput {
  name: string
  description?: string | null
  enabled: boolean
  /** One per screenful; camera_ids in tile order, null leaves a tile empty. */
  pages: Array<{ label: string; layout_id: string; duration_s: number; camera_ids: Slot[] }>
  settings?: TourSettings
  expected_revision?: number
}

export const tourService = {
  getTours: () => api.get('/api/v1/tours'),
  getTour: (tourId: number) => api.get(`/api/v1/tours/${tourId}`),
  createTour: (body: TourInput) => api.post('/api/v1/tours', body),
  updateTour: (tourId: number, body: TourInput) => api.put(`/api/v1/tours/${tourId}`, body),
  // On/off only: the pages are not re-sent, so nothing else in the tour changes.
  setTourEnabled: (tourId: number, body: { enabled: boolean; expected_revision?: number }) =>
    api.patch(`/api/v1/tours/${tourId}`, body),
  deleteTour: (tourId: number) => api.delete(`/api/v1/tours/${tourId}`),
  // A small cached still for the editor's camera picker (an <img> cannot
  // carry the bearer token, so it is fetched as a blob).
  getCameraThumbnail: (cameraId: number) =>
    api.get(`/api/v1/tours/thumbnails/${cameraId}`, { responseType: 'blob' }),
  // Stream info that also carries the low-res substream URL (webrtc_sub);
  // the plain getStreamInfo call is left exactly as it was.
  getTourStreamInfo: (cameraId: number) =>
    api.get(`/api/v1/streams/${cameraId}/info`, { params: { include_sub: true } }),
}
