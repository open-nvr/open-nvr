// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The host agent as the web app sees it (GET /api/v1/host-agent): the
 * process on the OpenNVR machine that streams USB webcams into their
 * cameras (server/services/host_agent.py, host-agent/).
 */

export type HostAgentWebcam = {
  id: string
  name: string
  assigned_camera_id: number | null
  assigned_camera_name: string | null
}

export type HostAgentStream = {
  camera_id: number
  state: 'starting' | 'live' | 'error' | 'device_missing' | 'waiting'
  detail?: string | null
}

export type HostAgentInfo = {
  installed: boolean
  connected: boolean
  last_seen: number | null
  version: string | null
  os: string | null
  hostname: string | null
  ffmpeg: { found: boolean; version?: string | null }
  webcams: HostAgentWebcam[]
  streams: HostAgentStream[]
}

/** The command that installs the agent, for the machine OpenNVR runs on. */
export function hostAgentInstallCommand(os: string | null | undefined): string {
  const guess = os || (typeof navigator !== 'undefined' && /windows/i.test(navigator.userAgent) ? 'windows' : 'linux')
  return guess === 'windows' ? '.\\start.ps1 host-agent install' : './start.sh host-agent install'
}

/** How to install ffmpeg, which the agent needs to capture. */
export function ffmpegInstallCommand(os: string | null | undefined): string {
  if (os === 'windows') return 'winget install --id Gyan.FFmpeg -e'
  if (os === 'darwin') return 'brew install ffmpeg'
  return 'sudo apt install ffmpeg'
}

/** A stream state in a few words, for a camera row or a tooltip. */
export function describeStream(s: HostAgentStream | undefined): string {
  if (!s) return 'Waiting for the host agent'
  switch (s.state) {
    case 'live': return 'Streaming'
    case 'starting': return 'Starting…'
    case 'device_missing': return 'Webcam not connected'
    case 'error': return s.detail ? `Error: ${s.detail}` : 'Error'
    default: return 'Waiting'
  }
}

/**
 * What an empty webcam tile should say instead of "No signal": a webcam has
 * no picture until something publishes into it, which is a normal state, not
 * a fault. undefined for every other camera (keeps "No signal").
 */
export function webcamOfflineLabel(cam: { source_type?: string | null; source_device?: string | null } | null | undefined): string | undefined {
  if (!cam || cam.source_type !== 'publisher') return undefined
  return cam.source_device ? 'Webcam starting…' : 'Waiting for the browser to publish'
}
