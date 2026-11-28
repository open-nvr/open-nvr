// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Live WebRTC connections kept warm between pages, for this tab.
 *
 * A live tile that leaves the screen (Live View → Dashboard) used to close
 * its connection; coming back built a new one: a new WHEP session, new ICE,
 * and on a network where WebRTC only gets through by NAT hole punching, a
 * new gamble that often lost. Now the leaving tile PARKS a healthy
 * connection here, and the next tile for the same stream ADOPTS it: the
 * picture is back in about a tenth of a second (Chromium keeps decoding a
 * track with no element attached), with no new session.
 *
 * A parked connection still receives the stream at full bitrate (WHEP has no
 * renegotiation to pause it), so parking is short and bounded: WARM_MS, at
 * most MAX_PARKED, all closed when the tab is hidden or on sign-out. HLS is
 * not parked (Chrome plays it natively in the element itself); the tile's
 * last-frame poster covers that case.
 *
 * Only the player (components/VideoPlayer) parks and adopts; the policy
 * helpers at the bottom are pure so the node tests cover them.
 */

import { transportKey } from './liveTransport'
import { isAdoptable, pickEvictions } from './liveStreamsPolicy'

/** How long a parked connection waits for a tile before it is closed. */
export const WARM_MS = 2 * 60 * 1000
/** At most this many parked connections; past it the oldest is closed. */
export const MAX_PARKED = 9

type Parked = {
  pc: RTCPeerConnection
  stream: MediaStream
  resource: string | null
  parkedAt: number
  timer: number
}

export type Adopted = { pc: RTCPeerConnection; stream: MediaStream; resource: string | null }

const parked = new Map<string, Parked>()

/** The registry key: the stream's WHEP path plus the camera's version, so a
 *  connection made before the camera was reconfigured is never adopted. */
export function parkKey(whepUrl: string | null | undefined, warmKey: string | number | null | undefined): string {
  const path = transportKey(whepUrl)
  return path ? `${path}#${warmKey ?? ''}` : ''
}

function end(entry: Parked) {
  window.clearTimeout(entry.timer)
  entry.pc.onconnectionstatechange = null
  try {
    entry.pc.close()
  } catch {
    // already closed
  }
  if (entry.resource) fetch(entry.resource, { method: 'DELETE' }).catch(() => {})
}

function drop(key: string) {
  const entry = parked.get(key)
  if (!entry) return
  parked.delete(key)
  end(entry)
}

let hooksInstalled = false
function installHooks() {
  if (hooksInstalled || typeof document === 'undefined') return
  hooksInstalled = true
  // A hidden tab shows nothing; a parked stream there is pure cost.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') closeAll()
  })
  window.addEventListener('pagehide', () => closeAll())
}

/** Keep `pc` warm under `key`. The caller has detached every handler of its
 *  own and must not touch `pc` again. A connection that is not connected is
 *  closed instead. */
export function park(key: string, pc: RTCPeerConnection, stream: MediaStream, resource: string | null) {
  if (!key || pc.connectionState !== 'connected' || typeof window === 'undefined') {
    end({ pc, stream, resource, parkedAt: 0, timer: 0 })
    return
  }
  installHooks()
  drop(key) // an older connection for the same stream: the newer one wins
  pc.ontrack = null
  pc.onconnectionstatechange = () => {
    const s = pc.connectionState
    if (s === 'disconnected' || s === 'failed' || s === 'closed') {
      if (parked.get(key)?.pc === pc) drop(key)
    }
  }
  const timer = window.setTimeout(() => {
    if (parked.get(key)?.pc === pc) drop(key)
  }, WARM_MS)
  parked.set(key, { pc, stream, resource, parkedAt: Date.now(), timer })
  for (const k of pickEvictions([...parked].map(([k, e]) => ({ key: k, parkedAt: e.parkedAt })), MAX_PARKED)) drop(k)
}

/** Is a healthy parked connection waiting under `key`? */
export function canAdopt(key: string): boolean {
  const e = key ? parked.get(key) : undefined
  return !!e && isAdoptable(e.pc.connectionState, e.stream.getVideoTracks().map((t) => t.readyState))
}

/** Take the parked connection under `key`, or null. The caller owns it from
 *  here: its handlers are cleared and its timers stopped. */
export function adopt(key: string): Adopted | null {
  if (!canAdopt(key)) {
    drop(key)
    return null
  }
  const e = parked.get(key)!
  parked.delete(key)
  window.clearTimeout(e.timer)
  e.pc.onconnectionstatechange = null
  return { pc: e.pc, stream: e.stream, resource: e.resource }
}

/** Close every parked connection (tab hidden, sign-out). */
export function closeAll() {
  for (const k of [...parked.keys()]) drop(k)
}

/** How many connections are parked (tests and diagnostics). */
export function parkedCount(): number {
  return parked.size
}
