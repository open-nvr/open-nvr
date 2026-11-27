/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Remote sites and their agents (server/routers/sites.py): a site's agent
 * relays the cameras on its LAN to this server over 443.
 */

import { api } from '../lib/api'

export type SitePublisher = {
  camera_id: number
  stream: 'main' | 'sub'
  state: 'starting' | 'live' | 'error' | 'waiting' | 'uplink_cap'
  detail?: string | null
  since?: number | null
  /** What the stream sends upstream, last 10 s. */
  kbps?: number | null
}

export type SiteCamera = {
  id: number
  name: string
  lan_host: string | null
  main_url: string | null
  sub_url: string | null
  is_active: boolean
}

export type DiscoveredDevice = {
  ip: string
  port?: number
  scheme?: string
  name?: string
  hardware?: string
  location?: string
}

export type Site = {
  id: number
  name: string
  created_at: string | null
  enrolled: boolean
  enrolled_at: string | null
  revoked_at: string | null
  credential_prefix: string | null
  code_pending: boolean
  code_expires_at: string | null
  online: boolean
  last_seen_at: string | null
  last_ip: string | null
  agent_version: string | null
  hostname: string | null
  publishers: SitePublisher[]
  cameras: SiteCamera[]
  discovered: DiscoveredDevice[]
  live_policy: string
  uplink_cap_kbps: number | null
  server_pin: string | null
}

export type EnrolCode = {
  enrol_code: string
  enrol_expires_at: string
  enrol_command: string
}

export type CameraLink = {
  main_url: string
  sub_url?: string | null
  username?: string | null
  password?: string | null
  /** Where the camera's control API (ONVIF, PTZ) answers, if not the stream's host. */
  control_host?: string | null
}

export const siteService = {
  list: async (): Promise<Site[]> => (await api.get('/api/v1/sites')).data,
  create: async (name: string): Promise<Site & EnrolCode> => (await api.post('/api/v1/sites', { name })).data,
  newCode: async (id: number): Promise<EnrolCode> => (await api.post(`/api/v1/sites/${id}/enrol-code`)).data,
  revoke: async (id: number): Promise<Site> => (await api.delete(`/api/v1/sites/${id}/credential`)).data,
  remove: async (id: number): Promise<void> => { await api.delete(`/api/v1/sites/${id}`) },
  link: async (id: number, cameraId: number, body: CameraLink): Promise<Site> =>
    (await api.put(`/api/v1/sites/${id}/cameras/${cameraId}`, body)).data,
  update: async (id: number, body: { name?: string; live_policy?: 'always' | 'sub_only'; uplink_cap_kbps?: number | null }): Promise<Site> =>
    (await api.patch(`/api/v1/sites/${id}`, body)).data,
  unlink: async (id: number, cameraId: number): Promise<Site> =>
    (await api.delete(`/api/v1/sites/${id}/cameras/${cameraId}`)).data,
  /** A pushed camera for a site: created like any camera, then linked. A link
   *  that fails takes the half-made camera away again. */
  addCamera: async (id: number, name: string, body: CameraLink): Promise<Site> => {
    const { data: cam } = await api.post('/api/v1/cameras/', { name, source_type: 'publisher' })
    try {
      return await siteService.link(id, cam.id, body)
    } catch (e) {
      await api.delete(`/api/v1/cameras/${cam.id}`).catch(() => {})
      throw e
    }
  },
}

/** The state shown for a site: what an operator needs to act on first. */
export function siteStatus(s: Site): { label: string; variant: 'success' | 'warning' | 'neutral' | 'destructive'; hint: string } {
  if (s.revoked_at && !s.enrolled) {
    return { label: 'Revoked', variant: 'destructive', hint: 'Its agent is cut off. Make a new code to enrol it again.' }
  }
  if (!s.enrolled) {
    return s.code_pending
      ? { label: 'Waiting for agent', variant: 'warning', hint: 'Run the enrol command on a machine at the site.' }
      : { label: 'Not enrolled', variant: 'neutral', hint: 'Make a code and run the enrol command at the site.' }
  }
  return s.online
    ? { label: 'Online', variant: 'success', hint: `Agent ${s.agent_version || ''} on ${s.hostname || 'the site'}` }
    : { label: 'Offline', variant: 'warning', hint: 'The agent has not checked in for a minute: the site, its uplink or the agent is down.' }
}

/** A camera's relay state at the site (its main stream). */
export function cameraRelayState(s: Site, cameraId: number): SitePublisher | undefined {
  return s.publishers.find((p) => p.camera_id === cameraId && p.stream === 'main')
}

/** What the site sends upstream now, in kbps (relayed streams, last 10 s). */
export function siteUplinkKbps(s: Site): number {
  return s.publishers.reduce((sum, p) => sum + (p.kbps || 0), 0)
}

/** An RTSP URL to start from for a discovered device. */
export function suggestedRtspUrl(d: DiscoveredDevice): string {
  return `rtsp://${d.ip}:554/`
}
