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

// The Vehicles page (first-class LPR vertical): plate reads off the
// canonical timeline store — every row a visit with plate_text and its
// best-frame evidence photo — plus the aggregates endpoint for the
// stat tiles. Owner-scoped server-side, same rule as everything else.
export const vehiclesService = {
  getPlateEvents: (params: {
    plate?: string
    camera_id?: number
    from?: string
    to?: string
    limit?: number
  }) => api.get('/api/v1/events', { params: { has_plate: true, ...params } }),

  getPlateStats: (days: number) =>
    api.get('/api/v1/events/plate-stats', { params: { days } }),

  // Everything the platform knows about ONE plate (all-time): first
  // seen, last seen, total reads, per-camera counts — the history
  // drill-down behind clicking a plate.
  getPlateSummary: (plate: string) =>
    api.get('/api/v1/events/plate-summary', { params: { plate } }),

  // Gate in / gate out. WHICH camera is which gate lives in the
  // providing app's config (gate_directions) — the endpoints take the
  // sets and stay stateless.
  getPlateSessions: (plate: string, inCameras: number[], outCameras: number[]) =>
    api.get('/api/v1/events/plate-sessions', {
      params: {
        plate,
        in_cameras: inCameras.join(','),
        out_cameras: outCameras.join(','),
      },
    }),
  getGateOccupancy: (inCameras: number[], outCameras: number[]) =>
    api.get('/api/v1/events/gate-occupancy', {
      params: {
        in_cameras: inCameras.join(','),
        out_cameras: outCameras.join(','),
      },
    }),

  // One calendar month of movement, aggregated for the printable
  // monthly report (registry join happens client-side).
  getVehicleReport: (year: number, month: number) =>
    api.get('/api/v1/events/vehicle-report', { params: { year, month } }),

  // Evidence photos are auth-gated (JWT header), so a bare <img src>
  // can't load them — fetch as a blob and objectURL it (AuthedImage).
  //
  // All four take an optional AbortSignal, which AuthedImage feeds from
  // react-query. Without it an abandoned request — a row scrolled away,
  // a filter changed mid-flight — still runs to completion on the
  // server, holding a connection for a picture nobody will look at.
  getEventEvidence: (eventId: number, signal?: AbortSignal) =>
    api.get(`/api/v1/events/${eventId}/evidence`, { responseType: 'blob', signal }),

  // The crop the PLATE was read from (#382). Multi-frame OCR reads
  // candidate crops, not the vehicle-best frame, so this is the only
  // image that actually shows the plate the row is captioned with.
  // 404s when the read came from the evidence frame itself or predates
  // the column — callers fall back to getEventEvidence.
  getEventPlateEvidence: (eventId: number, signal?: AbortSignal) =>
    api.get(`/api/v1/events/${eventId}/plate-evidence`, { responseType: 'blob', signal }),

  // The WHOLE frame the vehicle crop was cut from. /evidence is framed
  // for the subject — detection box plus a quarter-box margin — so it
  // cannot show the lane, the gate, or what the car was next to. 404s
  // for rows written before Tier-0 started sending it; callers fall
  // back to getEventEvidence.
  getEventSceneEvidence: (eventId: number, signal?: AbortSignal) =>
    api.get(`/api/v1/events/${eventId}/scene-evidence`, { responseType: 'blob', signal }),

  // The frame the plate crop was cut from. /evidence and
  // /scene-evidence show the visit's best-thumbnail moment, which can
  // be a DIFFERENT car when track association merged two vehicles into
  // one visit; this is the vehicle the number actually belongs to.
  getEventPlateFrame: (eventId: number, signal?: AbortSignal) =>
    api.get(`/api/v1/events/${eventId}/plate-frame`, { responseType: 'blob', signal }),
}
