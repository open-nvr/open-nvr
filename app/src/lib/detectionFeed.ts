// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// ONE WebSocket for every detection overlay on the page.
//
// Each live tile could open its own /events/ws filtered to its camera,
// but a 3×3 grid would then hold nine sockets and nine tickets for what
// is one stream of events. Instead this opens a single unfiltered socket
// — the server scopes it to the cameras the user is entitled to, per
// event, so "unfiltered" never means "everything on the site" — and fans
// the frames out to whichever overlays asked for that camera_id.
//
// Lifecycle follows demand: the socket opens when the first overlay
// subscribes and closes when the last one leaves, so a page with the
// overlay switched off costs nothing. Reconnects with backoff on drop; a
// fresh single-use ticket is fetched on every (re)connect, same as
// useCameraStatus, so the JWT never lands in a URL.

import { apiService } from './apiService'

export interface OverlayTrack {
  id: number | string | null
  label: string
  score: number
  /** Normalized [x, y, w, h] in 0..1 of the frame. */
  box: [number, number, number, number]
  stationary?: boolean
}

export interface OverlayFrame {
  cameraId: number
  /** Wall-clock ms when this frame arrived here (not the producer's stamp). */
  receivedAt: number
  calibrating: boolean
  tracks: OverlayTrack[]
}

type Listener = (frame: OverlayFrame) => void

const listeners = new Map<number, Set<Listener>>()
let ws: WebSocket | null = null
let connecting = false
let closedByDemand = false
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let reconnectAttempt = 0

function totalListeners(): number {
  let n = 0
  for (const set of listeners.values()) n += set.size
  return n
}

/** Accepts both the Tier-0 bridge shape (`tracks[].box` as [x,y,w,h]) and
 *  the adapter contract's detection shape (`detections[].bbox` as
 *  {x,y,w,h}), so a BYOM model's live results draw too. */
function toFrame(evt: any): OverlayFrame | null {
  const cameraId = Number(evt?.camera_id)
  if (!Number.isFinite(cameraId)) return null
  const p = evt?.payload ?? {}
  const out: OverlayTrack[] = []

  if (evt.event_type === 'tracks' && Array.isArray(p.tracks)) {
    for (const t of p.tracks) {
      const b = t?.box
      if (!Array.isArray(b) || b.length !== 4) continue
      out.push({
        id: t.id ?? null,
        label: String(t.label ?? 'object'),
        score: Number(t.score) || 0,
        box: [Number(b[0]), Number(b[1]), Number(b[2]), Number(b[3])],
        stationary: Boolean(t.stationary),
      })
    }
  } else if (evt.event_type === 'inference_result' && Array.isArray(p.detections)) {
    for (const d of p.detections) {
      const bb = d?.bbox
      let box: [number, number, number, number] | null = null
      if (bb && typeof bb === 'object' && !Array.isArray(bb)) {
        box = [Number(bb.x), Number(bb.y), Number(bb.w), Number(bb.h)]
      } else if (Array.isArray(bb) && bb.length === 4) {
        box = [Number(bb[0]), Number(bb[1]), Number(bb[2]), Number(bb[3])]
      }
      if (!box || box.some((v) => !Number.isFinite(v))) continue
      // Pixel-space boxes (any coordinate > 1) need the frame size; drop
      // them if it is absent rather than draw a box 1920 units wide.
      if (Math.max(...box) > 1) {
        const fw = Number(p.frame_dimensions?.w), fh = Number(p.frame_dimensions?.h)
        if (!(fw > 0 && fh > 0)) continue
        box = [box[0] / fw, box[1] / fh, box[2] / fw, box[3] / fh]
      }
      out.push({
        id: d.track_id ?? null,
        label: String(d.label ?? 'object'),
        score: Number(d.confidence) || 0,
        box,
      })
    }
  } else {
    return null
  }
  if (!out.length) return null
  return { cameraId, receivedAt: Date.now(), calibrating: Boolean(p.calibrating), tracks: out }
}

function scheduleReconnect() {
  if (closedByDemand || reconnectTimer) return
  const delay = Math.min(30_000, 1000 * 2 ** Math.min(reconnectAttempt, 5))
  reconnectAttempt += 1
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    if (totalListeners() > 0) void connect()
  }, delay)
}

async function connect() {
  if (ws || connecting) return
  connecting = true
  closedByDemand = false
  let ticket: string
  try {
    const res = await apiService.createEventsWsTicket()
    ticket = res.data.ticket
  } catch {
    connecting = false
    scheduleReconnect()
    return
  }
  if (totalListeners() === 0) { connecting = false; return }

  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  // task filter keeps everything the overlay does not draw off the wire:
  // tier0 = the platform detector, overlay = boxes an app asked to draw
  // (forwarded only for apps the operator switched on in the catalog).
  const url = `${proto}://${window.location.host}/api/v1/events/ws?ticket=${encodeURIComponent(ticket)}&task=tier0&task=overlay`
  const sock = new WebSocket(url)
  ws = sock
  connecting = false

  sock.onopen = () => { reconnectAttempt = 0 }
  sock.onmessage = (msg) => {
    let evt: any
    try { evt = JSON.parse(msg.data) } catch { return }
    const frame = toFrame(evt)
    if (!frame) return
    const set = listeners.get(frame.cameraId)
    if (!set) return
    for (const fn of set) { try { fn(frame) } catch { /* one bad listener must not stop the rest */ } }
  }
  sock.onclose = () => {
    if (ws === sock) ws = null
    if (!closedByDemand && totalListeners() > 0) scheduleReconnect()
  }
}

function disconnect() {
  closedByDemand = true
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  const sock = ws
  ws = null
  if (sock) { sock.onclose = null; sock.close() }
}

/** Subscribe to live frames for one camera. Returns the unsubscribe. */
export function subscribeDetections(cameraId: number, fn: Listener): () => void {
  let set = listeners.get(cameraId)
  if (!set) { set = new Set(); listeners.set(cameraId, set) }
  set.add(fn)
  void connect()
  return () => {
    const s = listeners.get(cameraId)
    if (s) { s.delete(fn); if (s.size === 0) listeners.delete(cameraId) }
    if (totalListeners() === 0) disconnect()
  }
}

/** Test seam: feed a raw WS event through the same parser the socket uses. */
export const _parseForTests = toFrame
