// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/** The pure rules of lib/liveStreams, kept import-free for the node tests. */

/** Keys to close so at most `max` stay parked: the oldest first. */
export function pickEvictions(entries: { key: string; parkedAt: number }[], max: number): string[] {
  if (entries.length <= max) return []
  return [...entries]
    .sort((a, b) => a.parkedAt - b.parkedAt)
    .slice(0, entries.length - max)
    .map((e) => e.key)
}

/** A parked connection is worth adopting only while ICE says connected and
 *  its video track is still live. */
export function isAdoptable(connectionState: string, videoTrackStates: string[]): boolean {
  return connectionState === 'connected' && videoTrackStates.length > 0 && videoTrackStates.every((s) => s === 'live')
}
