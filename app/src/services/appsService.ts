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

// App registry: detector apps built on the OpenNVR App SDK self-register on
// boot; the catalog enables/disables them and pushes manifest-driven config.
export const appsService = {
  getApps: () => api.get('/api/v1/apps'),
  getAppIndex: () => api.get('/api/v1/apps/index'),
  // RFC-0002 Phase 1: the platform skills registry — one derivation over
  // adapters, apps, assignments and health. The catalog reads it to show
  // the skill each installed app provides (status + published events).
  getSkills: () => api.get('/api/v1/skills'),
  enableApp: (id: string) => api.post(`/api/v1/apps/${id}/enable`),
  disableApp: (id: string) => api.post(`/api/v1/apps/${id}/disable`),
  updateAppConfig: (id: string, config: Record<string, any>) =>
    api.put(`/api/v1/apps/${id}/config`, config),
  getAppStatus: (id: string) => api.get(`/api/v1/apps/${id}/status`),

  // Opt-in one-click install: the agent GUIDES the operator, the backend
  // enforces opt-in + RBAC. install/uninstall 403 when APPS_INSTALL_ENABLED is
  // off OR the caller lacks apps.install — the UI degrades to command-display.
  // install-status is readable by any authed user regardless of that gating.
  installApp: (id: string) => api.post(`/api/v1/apps/index/${id}/install`),
  uninstallApp: (id: string) => api.post(`/api/v1/apps/index/${id}/uninstall`),
  getInstallStatus: (id: string) => api.get(`/api/v1/apps/index/${id}/install-status`),

  // Manifest-declared operator actions (search footage, enroll a face, …),
  // proxied through the server's user-JWT-only endpoint to the app's
  // contract surface. The service key can never invoke these.
  invokeAppAction: (id: string, action: string, params: Record<string, any>) =>
    api.post(`/api/v1/apps/${id}/actions/${action}`, params),
}
