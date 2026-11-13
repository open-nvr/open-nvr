// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Bounding boxes, labels and scores drawn over the live video.
//
// Client-side on a <canvas>, not burned into the stream. Frigate's
// "debug" view and ZoneMinder's zmeventnotification draw server-side into
// a second MJPEG stream — that costs a re-encode per viewer and a second
// stream per camera, and it means the clean recording and the annotated
// live view are different pixels. Scrypted, Shinobi, UniFi Protect and
// Blue Iris all overlay client-side, which is what a WebRTC/HLS
// architecture wants: one stream, boxes as data, drawn where they are
// looked at. It also makes the toggle free — no boxes means no canvas
// work and no socket.
//
// What the eye gets, and why:
//
// * Tier-0 publishes about five frames a second; the video plays at
//   twenty-five. A box redrawn five times a second jumps. Each track's
//   drawn box glides toward its latest box (an exponential approach with
//   a 90 ms time constant, run on the display's frame clock), so the box
//   moves with the person instead of after them.
// * A track that appears fades in over a few frames; one the detector has
//   lost fades out instead of vanishing. Tier-0 only publishes frames that
//   have tracks, so when the last one leaves no frame comes at all; the
//   stale sweep retires it.
// * Corner brackets with a faint outline and a 5 % tint, not a solid
//   rectangle: the subject stays visible through the box. The label pill
//   under the top-left corner carries the class, the track id and the
//   score as a number AND a thin bar, because 91 % and 51 % should look
//   different from across a room.
// * A short trail (the last 1.5 s of the box's foot point) on moving
//   tracks — direction of travel at a glance, and the proof that the
//   tracker follows one object rather than redetecting it.
// * A small badge in the corner: how many objects, how fast the detector
//   is publishing. Those two numbers are the live view's "AI is working".
//
// The canvas fills the player's "feed box", which VideoPlayer sizes to
// the stream's DISPLAY aspect and fills edge to edge with the video. So
// the canvas rectangle IS the video rectangle and normalized 0..1 boxes
// map straight onto it — no letterbox arithmetic, and an anamorphic
// stream that VideoPlayer un-squishes carries its boxes with it.

import { useEffect, useRef } from 'react'
import { subscribeDetections, type OverlayFrame, type OverlayTrack } from '../../lib/detectionFeed'

export interface DetectionOverlayProps {
  cameraId: number
  /** Drop a frame's boxes after this long without a newer one, so a
   *  camera whose detector paused does not show a stale box forever.
   *  Tier-0 publishes ~5 fps; two seconds covers a hiccup, not a stop. */
  staleMs?: number
  /** Show the object-count / fps badge in the corner. */
  badge?: boolean
}

// One hue per label, stable across frames so a "person" is always the
// same colour and the eye can follow it. Unlisted labels hash to a hue.
const LABEL_HUES: Record<string, number> = {
  person: 205, car: 30, truck: 45, bus: 45, motorcycle: 15, bicycle: 160,
  dog: 290, cat: 290, bird: 120, license_plate: 60, plate: 60, face: 330,
  backpack: 120, handbag: 120, suitcase: 120, umbrella: 60, cell_phone: 330,
}
function hueFor(label: string): number {
  if (label in LABEL_HUES) return LABEL_HUES[label]
  let h = 0
  for (let i = 0; i < label.length; i++) h = (h * 31 + label.charCodeAt(i)) % 360
  return h
}

/** Exponential approach: how far toward the target a value moves in `dt`
 *  ms with a `tau` ms time constant. 0 for no time, 1 for a long time. */
export function approach(dt: number, tau: number): number {
  if (dt <= 0) return 0
  return 1 - Math.exp(-dt / tau)
}

const SMOOTH_TAU_MS = 90
const FADE_IN_MS = 180
const FADE_OUT_MS = 420
const TRAIL_MS = 1500
const TRAIL_MIN_MOVE = 0.004  // normalized; below this the foot point is noise

interface Shown {
  track: OverlayTrack
  box: [number, number, number, number]       // what is drawn now
  target: [number, number, number, number]    // what Tier-0 last said
  seenAt: number                              // last frame that carried it
  bornAt: number
  lostAt: number | null
  trail: { x: number, y: number, at: number }[]
}

export function DetectionOverlay({ cameraId, staleMs = 2000, badge = true }: DetectionOverlayProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const frameRef = useRef<OverlayFrame | null>(null)
  const shownRef = useRef<Map<string, Shown>>(new Map())
  const rafRef = useRef<number | null>(null)
  const lastTickRef = useRef<number>(0)
  const fpsRef = useRef<{ at: number, fps: number }>({ at: 0, fps: 0 })

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const shown = shownRef.current
    shown.clear()

    // Size the backing store to the element's CSS size × devicePixelRatio,
    // so lines are crisp on HiDPI and never smeared by CSS scaling.
    const fit = () => {
      const dpr = window.devicePixelRatio || 1
      const w = Math.max(1, Math.round(canvas.clientWidth * dpr))
      const h = Math.max(1, Math.round(canvas.clientHeight * dpr))
      if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h }
    }
    const ro = new ResizeObserver(fit)
    ro.observe(canvas)
    fit()

    const keyOf = (t: OverlayTrack, i: number) => (t.id != null ? `${t.label}#${t.id}` : `${t.label}@${i}`)

    // A new frame: retarget the tracks it carries, mark the rest lost.
    const ingest = (frame: OverlayFrame) => {
      const now = frame.receivedAt
      const live = new Set<string>()
      frame.tracks.forEach((t, i) => {
        const key = keyOf(t, i)
        live.add(key)
        const s = shown.get(key)
        if (s) {
          s.track = t
          s.target = t.box
          s.seenAt = now
          s.lostAt = null
        } else {
          shown.set(key, { track: t, box: t.box, target: t.box, seenAt: now, bornAt: now, lostAt: null, trail: [] })
        }
      })
      for (const [key, s] of shown) if (!live.has(key) && s.lostAt == null) s.lostAt = now
      // Publishing rate, smoothed: the badge's "fps".
      const f = fpsRef.current
      if (f.at) {
        const inst = 1000 / Math.max(40, now - f.at)
        f.fps = f.fps ? f.fps * 0.8 + inst * 0.2 : inst
      }
      f.at = now
    }

    const tick = () => {
      rafRef.current = null
      fit()
      const now = Date.now()
      const dt = lastTickRef.current ? Math.min(100, now - lastTickRef.current) : 16
      lastTickRef.current = now
      const W = canvas.width, H = canvas.height
      ctx.clearRect(0, 0, W, H)

      const frame = frameRef.current
      const frameStale = !frame || now - frame.receivedAt > staleMs
      const k = approach(dt, SMOOTH_TAU_MS)
      let alive = 0

      for (const [key, s] of shown) {
        // Retire: lost and faded, or the whole feed went quiet.
        if (frameStale && s.lostAt == null) s.lostAt = now
        if (s.lostAt != null && now - s.lostAt > FADE_OUT_MS) { shown.delete(key); continue }
        // Glide toward the latest box.
        for (let i = 0; i < 4; i++) s.box[i] += (s.target[i] - s.box[i]) * k
        // Foot point trail, trimmed to the window.
        const fx = s.box[0] + s.box[2] / 2, fy = s.box[1] + s.box[3]
        const last = s.trail[s.trail.length - 1]
        if (!last || Math.hypot(fx - last.x, fy - last.y) > TRAIL_MIN_MOVE) s.trail.push({ x: fx, y: fy, at: now })
        while (s.trail.length && now - s.trail[0].at > TRAIL_MS) s.trail.shift()
        const fadeIn = Math.min(1, (now - s.bornAt) / FADE_IN_MS)
        const fadeOut = s.lostAt == null ? 1 : Math.max(0, 1 - (now - s.lostAt) / FADE_OUT_MS)
        const alpha = fadeIn * fadeOut * (frame?.calibrating ? 0.6 : 1)
        if (s.lostAt == null) alive++
        drawTrack(ctx, s, W, H, alpha)
      }
      if (badge && !frameStale) drawBadge(ctx, W, alive, fpsRef.current.fps)
      if (shown.size) requestTick()
    }
    const requestTick = () => { if (rafRef.current == null) rafRef.current = requestAnimationFrame(tick) }

    const unsubscribe = subscribeDetections(cameraId, (frame) => {
      frameRef.current = frame
      ingest(frame)
      requestTick()
    })
    // Expire stale boxes even when no new frame arrives to trigger a draw.
    const sweep = window.setInterval(() => {
      const f = frameRef.current
      if (f && Date.now() - f.receivedAt > staleMs) requestTick()
    }, 250)

    return () => {
      unsubscribe()
      window.clearInterval(sweep)
      ro.disconnect()
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current)
      shown.clear()
      ctx.clearRect(0, 0, canvas.width, canvas.height)
    }
  }, [cameraId, staleMs, badge])

  return (
    <canvas
      ref={canvasRef}
      className="absolute inset-0 w-full h-full pointer-events-none"
      aria-hidden="true"
      data-testid="detection-overlay"
    />
  )
}

function roundedRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  const rr = Math.min(r, w / 2, h / 2)
  ctx.beginPath()
  ctx.moveTo(x + rr, y)
  ctx.arcTo(x + w, y, x + w, y + h, rr)
  ctx.arcTo(x + w, y + h, x, y + h, rr)
  ctx.arcTo(x, y + h, x, y, rr)
  ctx.arcTo(x, y, x + w, y, rr)
  ctx.closePath()
}

function drawTrack(ctx: CanvasRenderingContext2D, s: Shown, W: number, H: number, alpha: number) {
  const t = s.track
  const [nx, ny, nw, nh] = s.box
  const x = nx * W, y = ny * H, w = nw * W, h = nh * H
  if (!(w > 0 && h > 0) || alpha <= 0.01) return
  const dpr = window.devicePixelRatio || 1
  const hue = hueFor(t.label)
  const stationary = !!t.stationary
  const a = alpha * (stationary ? 0.75 : 1)
  const lw = Math.max(1.5, 2.2 * dpr)
  const stroke = `hsla(${hue}, 90%, 62%, ${a})`

  // Trail first, under everything: the path the foot point took.
  if (s.trail.length > 2 && !stationary) {
    ctx.lineCap = 'round'
    ctx.lineJoin = 'round'
    for (let i = 1; i < s.trail.length; i++) {
      const p0 = s.trail[i - 1], p1 = s.trail[i]
      const age = 1 - (Date.now() - p1.at) / TRAIL_MS
      ctx.strokeStyle = `hsla(${hue}, 90%, 62%, ${0.55 * age * a})`
      ctx.lineWidth = Math.max(1, 3 * dpr * age)
      ctx.beginPath()
      ctx.moveTo(p0.x * W, p0.y * H)
      ctx.lineTo(p1.x * W, p1.y * H)
      ctx.stroke()
    }
  }

  // Tint + faint outline: the subject stays visible through the box.
  const r = Math.min(8 * dpr, w / 4, h / 4)
  roundedRect(ctx, x, y, w, h, r)
  ctx.fillStyle = `hsla(${hue}, 90%, 60%, ${0.07 * a})`
  ctx.fill()
  ctx.lineWidth = Math.max(1, 1 * dpr)
  ctx.strokeStyle = `hsla(${hue}, 90%, 62%, ${0.35 * a})`
  ctx.stroke()

  // Corner brackets: a dark halo under the coloured stroke keeps them
  // legible on both bright sky and dark tarmac.
  const len = Math.min(Math.min(w, h) * 0.28, 30 * dpr)
  const corners: [number, number, number, number][] = [
    [x, y, 1, 1], [x + w, y, -1, 1], [x, y + h, 1, -1], [x + w, y + h, -1, -1],
  ]
  ctx.lineCap = 'round'
  for (const pass of [0, 1]) {
    ctx.lineWidth = pass === 0 ? lw + 2.5 : lw
    ctx.strokeStyle = pass === 0 ? `rgba(0,0,0,${0.5 * a})` : stroke
    for (const [cx, cy, sx, sy] of corners) {
      ctx.beginPath()
      ctx.moveTo(cx, cy + sy * len)
      ctx.lineTo(cx, cy)
      ctx.lineTo(cx + sx * len, cy)
      ctx.stroke()
    }
  }

  // Label pill: "person #12" and the score as a number and a bar. Below
  // the top-left corner inside the box when there is room above the
  // frame edge issue, else above the box.
  const fontPx = Math.max(10, Math.round(12 * dpr))
  ctx.font = `600 ${fontPx}px system-ui, -apple-system, sans-serif`
  ctx.textBaseline = 'top'
  const idPart = t.id != null ? `  #${t.id}` : ''
  const pct = t.score == null ? '' : `  ${Math.round(t.score * 100)}%`
  const text = `${t.label.replace(/_/g, ' ')}${idPart}${pct}`
  const padX = Math.round(fontPx * 0.55), padY = Math.round(fontPx * 0.32)
  const tw = ctx.measureText(text).width
  const chipW = tw + padX * 2, chipH = fontPx + padY * 2 + (t.score == null ? 0 : 3 * dpr)
  const gap = 4 * dpr
  const chipX = Math.min(Math.max(0, x), W - chipW)
  const chipY = y - chipH - gap >= 0 ? y - chipH - gap : y + gap
  roundedRect(ctx, chipX, chipY, chipW, chipH, 6 * dpr)
  ctx.fillStyle = `rgba(8, 12, 22, ${0.82 * a})`
  ctx.fill()
  ctx.lineWidth = Math.max(1, 1 * dpr)
  ctx.strokeStyle = `hsla(${hue}, 90%, 62%, ${0.8 * a})`
  ctx.stroke()
  ctx.fillStyle = `rgba(255,255,255,${a})`
  ctx.fillText(text, chipX + padX, chipY + padY)
  if (t.score != null) {
    const barY = chipY + chipH - 3 * dpr, barW = chipW - padX * 2
    ctx.fillStyle = `rgba(255,255,255,${0.18 * a})`
    ctx.fillRect(chipX + padX, barY, barW, 2 * dpr)
    ctx.fillStyle = `hsla(${hue}, 90%, 62%, ${a})`
    ctx.fillRect(chipX + padX, barY, barW * Math.max(0, Math.min(1, t.score)), 2 * dpr)
  }
}

function drawBadge(ctx: CanvasRenderingContext2D, W: number, objects: number, fps: number) {
  const dpr = window.devicePixelRatio || 1
  const fontPx = Math.max(10, Math.round(11 * dpr))
  ctx.font = `600 ${fontPx}px system-ui, -apple-system, sans-serif`
  ctx.textBaseline = 'top'
  const text = `${objects}  ·  ${fps ? fps.toFixed(0) : '–'} fps`
  const padX = Math.round(fontPx * 0.6), padY = Math.round(fontPx * 0.35)
  const dot = fontPx * 0.55
  const tw = ctx.measureText(text).width
  const w = tw + padX * 2 + dot + padX * 0.6, h = fontPx + padY * 2
  const x = W - w - 8 * dpr, y = 8 * dpr
  roundedRect(ctx, x, y, w, h, h / 2)
  ctx.fillStyle = 'rgba(8, 12, 22, 0.72)'
  ctx.fill()
  ctx.lineWidth = Math.max(1, 1 * dpr)
  ctx.strokeStyle = 'rgba(120, 190, 255, 0.45)'
  ctx.stroke()
  // The pulse: a live detector, not a static badge.
  const pulse = 0.55 + 0.45 * Math.sin(Date.now() / 350)
  ctx.beginPath()
  ctx.arc(x + padX + dot / 2, y + h / 2, dot / 2, 0, Math.PI * 2)
  ctx.fillStyle = `rgba(80, 220, 140, ${pulse})`
  ctx.fill()
  ctx.fillStyle = 'rgba(235, 242, 255, 0.95)'
  ctx.fillText(text, x + padX + dot + padX * 0.6, y + padY)
}
