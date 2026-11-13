// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// OpenNVR updates (server/routers/system_update.py). Not the host OS
// updates of the Firmware page.

import { api } from '../lib/api'

export type UpdateMode = 'off' | 'notify' | 'manual' | 'auto'

export interface UpdatePolicy {
  mode: UpdateMode
  channel: 'stable'
  auto_scope: 'patch' | 'minor'
  /** Comma list of mon..sun, site-local. */
  window_days: string
  window_start: string
  window_end: string
}

export interface UpdateStatus {
  installed: { version: string; commit: string | null }
  available: { version: string; notes_url: string | null; urgent: boolean; manual_steps: string[]; state_migrations: boolean } | null
  last_check: { at: string | null; result: 'available' | 'up_to_date' | 'refused' | 'needs_operator' | 'error'; message: string } | null
  run: { from: string | null; to: string | null; step: string; started: string | null; finished: string | null; in_progress: boolean } | null
  history: Array<{ finished: string; from: string; to: string; result: string }>
  runner: { last_seen: string | null; running: boolean }
  pending_request: { to: string; requested_by: string; requested_at: string } | null
  /** The hand-off folders are mounted (an install brought up by start.sh / start.ps1). */
  connected: boolean
  policy: UpdatePolicy
}

export const updateService = {
  getUpdateStatus: () => api.get('/api/v1/system/update'),
  setUpdatePolicy: (policy: UpdatePolicy) => api.put('/api/v1/system/update/policy', policy),
  requestUpdate: (version: string) => api.post('/api/v1/system/update/request', { version }),
  cancelUpdateRequest: () => api.delete('/api/v1/system/update/request'),
}
