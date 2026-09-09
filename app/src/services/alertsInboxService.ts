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

// The operator alert inbox: app-emitted §11.5 alerts landed by the
// core's opennvr.alerts.> consumer. The bell polls unacked rows, rings
// per severity (ring-config), and acknowledges back so every open
// browser goes quiet together.

export type InboxAlert = {
  id: number
  alert_id: string
  fired_at: string | null
  // When the thing the alert is ABOUT was seen, when the producer knew
  // it. Null otherwise — show fired_at then. fired_at remains the sort
  // key server-side, so the inbox stays append-only.
  observed_at: string | null
  severity: 'low' | 'medium' | 'high' | 'critical'
  title: string
  description: string | null
  source_kind: string | null
  source_name: string | null
  camera_id: string | null
  correlation_id: string | null
  evidence: Record<string, unknown> | null
  tags: string[]
  acknowledged_at: string | null
  acknowledged_by: number | null
}

// When the thing happened, not when the app noticed.
//
// `observed_at` is when the thing the alert is ABOUT was seen; `fired_at`
// is when the producing app finished deciding, which trails it by however
// long inference and the bus took. Showing fired_at as THE time is what
// made one plate read display two different clocks — the alarm tables and
// the vehicle list disagreed by seconds that grew with OCR backlog (#451).
//
// fired_at still ORDERS the inbox (the server sorts by id, i.e. arrival),
// so a slow read cannot insert its alarm above ones already on screen.
export function alarmSeenAt(a: InboxAlert): string {
  const seen = a.observed_at ?? a.fired_at
  return seen ? new Date(seen).toLocaleString() : '—'
}

// Tooltip for the cell above. When the two differ by a noticeable margin
// the lag rides along: it is a useful health signal, and hiding it would
// be the same dishonesty in the other direction.
export function alarmSeenTitle(a: InboxAlert): string | undefined {
  if (!a.observed_at || !a.fired_at) return undefined
  const lagMs = new Date(a.fired_at).getTime() - new Date(a.observed_at).getTime()
  if (!Number.isFinite(lagMs) || lagMs < 1000) return undefined
  const at = new Date(a.fired_at).toLocaleTimeString()
  return `Alerted ${Math.round(lagMs / 1000)}s later, at ${at}`
}

export type RingMode = 'none' | 'ping' | 'continuous'
export type RingConfig = Record<'low' | 'medium' | 'high' | 'critical', RingMode>

export const alertsInboxService = {
  listInboxAlerts: (params?: {
    unacked?: boolean
    severity?: string
    source_name?: string
    after_id?: number
    limit?: number
  }) => api.get('/api/v1/alerts-inbox', { params }),

  // ids omitted/undefined = acknowledge everything unacked.
  ackInboxAlerts: (ids?: number[]) =>
    api.post('/api/v1/alerts-inbox/ack', ids ? { ids } : {}),

  getRingConfig: () => api.get('/api/v1/alerts-inbox/ring-config'),
  putRingConfig: (ring: RingConfig) =>
    api.put('/api/v1/alerts-inbox/ring-config', { ring }),

  // Beyond the browser: phone call / SMS (Twilio) + hooter relay.
  getAlarmActions: () => api.get('/api/v1/alerts-inbox/actions'),
  putAlarmActions: (actions: Record<string, unknown>) =>
    api.put('/api/v1/alerts-inbox/actions', actions),
  testAlarmActions: () => api.post('/api/v1/alerts-inbox/actions/test', {}),
}
