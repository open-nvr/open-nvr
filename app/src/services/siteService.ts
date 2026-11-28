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
  /** The agent's own relay, or ffmpeg (its fallback for a camera it cannot handle). */
  relay?: 'native' | 'ffmpeg' | null
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
  /** A replaced agent waiting for an admin's approval: it gets no cameras yet. */
  approval_pending?: boolean
  pending_agent?: { hostname: string | null; ip: string | null; version: string | null; credential_prefix: string | null } | null
}

/** Whether this server can hand out setup links (a public, trusted certificate). */
export type SetupLinkStatus = {
  ready: boolean
  reason: 'no_public_hostname' | 'certificate_not_trusted' | 'front_proxy_ends_tls' | null
  detail: string
  host: string | null
}

export type SetupLink = {
  token_id: string
  state: 'active' | 'used' | 'revoked' | 'expired'
  created_at: string | null
  expires_at: string
  used_at: string | null
  used_ip: string | null
  used_hostname: string | null
  /** Only when just made: the link (its secret is in the #fragment) and the code. */
  url?: string
  setup_code?: string
}

/** What the public setup page learns about a link (never its secret). */
export type SetupInfo = {
  site_name: string
  expires_at: string
  build: string | null
  files: { platform: string; filename: string; size: number; sha256: string }[]
}

/** Copy-paste setup for one OS: fetch the agent from this server, enrol, start. */
export type SetupCommands = {
  os: 'linux' | 'windows'
  label: string
  download: string
  enrol: string
  run: string
}

export type EnrolCode = {
  enrol_code: string
  enrol_expires_at: string
  enrol_command: string
  /** Absent from servers older than the download route. */
  setup?: SetupCommands[]
}

/** Builds of the agent this server serves at /api/v1/site-agent/download/<platform>. */
export const AGENT_DOWNLOADS: { platform: string; label: string }[] = [
  { platform: 'windows-amd64', label: 'Windows (x64)' },
  { platform: 'linux-amd64', label: 'Linux (x64)' },
  { platform: 'linux-arm64', label: 'Linux (ARM64, Raspberry Pi 4/5)' },
  { platform: 'linux-arm', label: 'Linux (32-bit ARM, Raspberry Pi OS 32-bit)' },
  { platform: 'darwin-arm64', label: 'macOS (Apple silicon)' },
  { platform: 'darwin-amd64', label: 'macOS (Intel)' },
]

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
  setupStatus: async (refresh = false): Promise<SetupLinkStatus> =>
    (await api.get('/api/v1/sites/setup-links/status', { params: refresh ? { refresh: true } : {} })).data,
  newSetupLink: async (id: number, hours: number): Promise<SetupLink> =>
    (await api.post(`/api/v1/sites/${id}/setup-links`, { hours })).data,
  setupLinks: async (id: number): Promise<SetupLink[]> => (await api.get(`/api/v1/sites/${id}/setup-links`)).data,
  revokeSetupLink: async (id: number, tokenId: string): Promise<void> => {
    await api.delete(`/api/v1/sites/${id}/setup-links/${encodeURIComponent(tokenId)}`)
  },
  approveAgent: async (id: number): Promise<Site> => (await api.post(`/api/v1/sites/${id}/approve-agent`)).data,
  /** Public (no login): what a setup link is for. Sends the link's id only. */
  setupInfo: async (tokenId: string): Promise<SetupInfo> =>
    (await api.post('/api/v1/site-agent/setup-info', { token_id: tokenId })).data,
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
