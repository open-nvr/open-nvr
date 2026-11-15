// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The channels of an ONVIF device, as POST /connect reports them (server:
 * services/onvif_digest_service.group_channels). An IP camera is one
 * channel; an NVR is one per video source. The Add Camera dialog shows a
 * checklist for the latter and adds the ticked channels in one go through
 * POST /cameras/bulk. Pure functions here so they can be tested without
 * the dialog.
 */

export type OnvifProfile = {
  token: string
  name: string
  stream_uri?: string | null
  width?: number
  height?: number
  video_source_token?: string
}

export type OnvifChannel = {
  index: number
  name: string
  source_token: string | null
  main: OnvifProfile | null
  sub: OnvifProfile | null
  profiles: OnvifProfile[]
  already_added?: boolean
  already_added_camera?: { id: number; name: string } | null
}

export type DeviceIdentity = {
  manufacturer?: string
  model?: string
  firmwareversion?: string
  serialnumber?: string
  hardwareid?: string
}

export type BulkCameraRow = {
  name: string
  ip_address: string
  port: number
  username?: string
  password?: string
  rtsp_url: string
  substream_url?: string
  manufacturer?: string
  model?: string
  firmware_version?: string
  serial_number?: string
  hardware_id?: string
}

/** More than one video source: the device is a recorder (or a multi-sensor camera). */
export function isMultiChannel(channels: OnvifChannel[] | undefined | null): boolean {
  return (channels?.length ?? 0) > 1
}

/** A channel can be added when its main stream has a URI. */
export function channelAddable(ch: OnvifChannel): boolean {
  return Boolean(ch.main?.stream_uri)
}

/** Ticked by default: every addable channel not already on the system. */
export function defaultChannelSelection(channels: OnvifChannel[]): Set<number> {
  const out = new Set<number>()
  for (const ch of channels) {
    if (channelAddable(ch) && !ch.already_added) out.add(ch.index)
  }
  return out
}

/** "<prefix> Ch <n>", within the camera name limit. */
export function channelCameraName(prefix: string, index: number): string {
  const p = prefix.trim()
  return `${p ? p + ' ' : ''}Ch ${index}`.slice(0, 100)
}

/** The resolution label for a channel's main stream, or ''. */
export function channelSizeLabel(ch: OnvifChannel): string {
  const m = ch.main
  return m?.width && m?.height ? `${m.width}×${m.height}` : ''
}

/**
 * The rows POST /cameras/bulk takes for the ticked channels. URLs go as the
 * device gave them, without a login: the server embeds the username and
 * password (and URL-encodes them), as it does for a camera added by hand.
 */
export function bulkRowsForChannels(
  channels: OnvifChannel[],
  selected: Set<number>,
  prefix: string,
  ip: string,
  credentials: { username: string; password: string },
  identity: DeviceIdentity | null | undefined,
): BulkCameraRow[] {
  const rows: BulkCameraRow[] = []
  for (const ch of channels) {
    if (!selected.has(ch.index) || !ch.main?.stream_uri) continue
    rows.push({
      name: channelCameraName(prefix, ch.index),
      ip_address: ip,
      port: 554,
      username: credentials.username || undefined,
      password: credentials.password || undefined,
      rtsp_url: ch.main.stream_uri,
      substream_url: ch.sub?.stream_uri || undefined,
      manufacturer: identity?.manufacturer || undefined,
      model: identity?.model || undefined,
      firmware_version: identity?.firmwareversion || undefined,
      serial_number: identity?.serialnumber || undefined,
      hardware_id: identity?.hardwareid || undefined,
    })
  }
  return rows
}

/** The id of the lowest-channel camera a bulk job created, if any yet. */
export function firstCreatedCameraId(
  results: Array<{ channel: number | null; outcome: string; camera_id: number | null }> | undefined,
): number | undefined {
  const created = (results ?? [])
    .filter((r) => r.outcome === 'created' && r.camera_id != null)
    .sort((a, b) => (a.channel ?? 0) - (b.channel ?? 0))
  return created[0]?.camera_id ?? undefined
}
