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

  // Evidence photos are auth-gated (JWT header), so a bare <img src>
  // can't load them — fetch as a blob and objectURL it (AuthedImage).
  getEventEvidence: (eventId: number) =>
    api.get(`/api/v1/events/${eventId}/evidence`, { responseType: 'blob' }),
}
