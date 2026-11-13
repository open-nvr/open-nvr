// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Alarm takeover for a running tour, as pure functions (tested in
 * app/tests/tourAlerts.test.mjs; hooks/useTourAlerts.ts feeds them).
 *
 * An alert on a camera brings it to the front of the wall for `hold_s`;
 * the tour then carries on where it was. Up to MAX_ACTIVE cameras share
 * the takeover, newest first. A camera already on screen that alarms again
 * stays longer instead of taking a second tile; once its takeover ends it
 * cannot take over again for `cooldown_s`, so one busy camera cannot hold
 * the wall.
 */

import type { TourAlertSettings, AlertSeverity } from '../services/tourService'

export const MAX_ACTIVE = 4

const SEVERITY_RANK: Record<AlertSeverity, number> = { low: 0, medium: 1, high: 2, critical: 3 }

/** A frame from /api/v1/events/ws, the fields this needs. */
export interface BusEvent {
  event_type?: string
  camera_id?: number | null
  task?: string | null
  payload?: Record<string, unknown> | null
}

export interface ActiveAlert {
  cameraId: number
  /** What happened, in a few words ("Person in zone", "Motion"). */
  title: string
  severity: AlertSeverity | null
  at: number
  until: number
}

/** Camera-event tasks that are housekeeping, not alarms. */
const NOT_ALARMS = new Set(['recording_stalled', 'recording_resumed', 'recording_degraded', 'connection'])

const words = (s: string) => {
  const w = s.replace(/[_.-]+/g, ' ').trim()
  return w ? w[0].toUpperCase() + w.slice(1) : ''
}

/**
 * The alert this event raises under `settings`, or null when it raises
 * none: wrong source, a camera outside the scope, a low severity, or
 * housekeeping. `tourCameras` = every camera in the tour.
 */
export function matchAlert(
  evt: BusEvent,
  settings: TourAlertSettings,
  tourCameras: ReadonlySet<number>,
): { cameraId: number; title: string; severity: AlertSeverity | null } | null {
  if (!settings.enabled) return null
  const source = evt.event_type
  if (source !== 'app_alert' && source !== 'camera_event') return null
  if (!settings.sources.includes(source)) return null
  const cameraId = typeof evt.camera_id === 'number' ? evt.camera_id : null
  if (cameraId == null) return null
  if (settings.scope === 'tour' && !tourCameras.has(cameraId)) return null
  const p = evt.payload ?? {}
  if (source === 'camera_event') {
    const task = String(evt.task ?? '')
    if (!task || NOT_ALARMS.has(task)) return null
    return { cameraId, title: words(task), severity: null }
  }
  const sev = String(p.severity ?? 'high').toLowerCase() as AlertSeverity
  const rank = SEVERITY_RANK[sev] ?? SEVERITY_RANK.high
  if (rank < SEVERITY_RANK[settings.min_severity]) return null
  const title = typeof p.title === 'string' && p.title.trim()
    ? p.title.trim()
    : words(String(p.alert_type ?? evt.task ?? 'Alert'))
  return { cameraId, title, severity: SEVERITY_RANK[sev] != null ? sev : 'high' }
}

/**
 * The takeover after `hit` arrives at `now`. `lastEnded` = when each
 * camera's previous takeover ended (for the cool-down); returns the new
 * list, or the same list when the hit is cooling down.
 */
export function addAlert(
  active: readonly ActiveAlert[],
  hit: { cameraId: number; title: string; severity: AlertSeverity | null },
  now: number,
  settings: TourAlertSettings,
  lastEnded: ReadonlyMap<number, number>,
): ActiveAlert[] {
  const holdMs = settings.hold_s * 1000
  const existing = active.find((a) => a.cameraId === hit.cameraId)
  if (existing) {
    return [{ ...existing, ...hit, at: now, until: now + holdMs }, ...active.filter((a) => a !== existing)]
  }
  const ended = lastEnded.get(hit.cameraId)
  if (ended != null && now - ended < settings.cooldown_s * 1000) return active as ActiveAlert[]
  return [{ ...hit, at: now, until: now + holdMs }, ...active].slice(0, MAX_ACTIVE)
}

/** Split into what is still on screen and what has run out at `now`. */
export function expire(active: readonly ActiveAlert[], now: number): { kept: ActiveAlert[]; ended: ActiveAlert[] } {
  const kept: ActiveAlert[] = []
  const ended: ActiveAlert[] = []
  for (const a of active) (a.until > now ? kept : ended).push(a)
  return { kept, ended }
}
