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
  /** Apps released from the registry. '' = the same as mode / auto_scope. */
  apps_mode: '' | UpdateMode
  apps_auto_scope: '' | 'patch' | 'minor'
  /** One app's mode, over both. */
  app_modes: Record<string, UpdateMode>
}

/** The newest release of an app the host found (updater/apps.py). */
export interface AppOffer {
  version: string
  kind: 'patch' | 'minor' | 'major'
  /** manual: changes the app's data so the old version cannot read it; never installed by itself. */
  rollback: 'safe' | 'manual'
  /** The host's cosign check against the signer the catalog names. */
  signature: 'ok' | 'failed' | 'unchecked'
  signature_message: string
  /** It did not come up healthy here and was rolled back; not retried by itself. */
  failed_before: boolean
}

export interface AppUpdate {
  id: string
  state: 'not_installed' | 'unmanaged' | 'up_to_date' | 'available' | 'blocked' | 'error' | 'off'
  reason: string
  installed_version: string | null
  installed_revoked: string | null
  available: AppOffer | null
  skipped: string[]
}

export interface AppsUpdateStatus {
  apps: AppUpdate[]
  problems: string[]
  last_check: { at: string | null; ok: boolean; message: string } | null
  run: { app: string | null; from: string | null; to: string | null; step: string; started: string | null; finished: string | null; in_progress: boolean; message?: string } | null
  history: Array<{ finished: string; app: string; from: string; to: string; result: string }>
  pending_request: { app: string; requested_by: string; requested_at: string } | null
}

export interface UpdateStatus {
  installed: { version: string; commit: string | null }
  available: { version: string; notes_url: string | null; urgent: boolean; manual_steps: string[]; state_migrations: boolean } | null
  last_check: { at: string | null; result: 'available' | 'up_to_date' | 'refused' | 'needs_operator' | 'error'; message: string } | null
  run: { from: string | null; to: string | null; step: string; started: string | null; finished: string | null; in_progress: boolean; message?: string } | null
  history: Array<{ finished: string; from: string; to: string; result: string }>
  runner: { last_seen: string | null; running: boolean }
  pending_request: { to: string; requested_by: string; requested_at: string } | null
  /** A Check now the host runner has not picked up yet. */
  pending_check?: { requested_by: string; requested_at: string } | null
  /** The hand-off folders are mounted (an install brought up by start.sh / start.ps1). */
  connected: boolean
  policy: UpdatePolicy
  /** Apps released from the registry (docs/design/app-updates-from-registry.md). */
  apps: AppsUpdateStatus
}

export const updateService = {
  getUpdateStatus: () => api.get('/api/v1/system/update'),
  setUpdatePolicy: (policy: UpdatePolicy) => api.put('/api/v1/system/update/policy', policy),
  requestUpdate: (version: string) => api.post('/api/v1/system/update/request', { version }),
  cancelUpdateRequest: () => api.delete('/api/v1/system/update/request'),
  requestAppUpdate: (appId: string) => api.post(`/api/v1/system/update/apps/${encodeURIComponent(appId)}/request`),
  cancelAppUpdateRequest: () => api.delete('/api/v1/system/update/apps/request'),
  /** Check now: the host looks for new OpenNVR and app releases within a minute. */
  requestUpdateCheck: () => api.post('/api/v1/system/update/check'),
}
