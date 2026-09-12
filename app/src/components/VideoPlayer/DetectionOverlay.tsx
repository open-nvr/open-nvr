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
}

// One hue per label, stable across frames so a "person" is always the
// same colour and the eye can follow it. Unlisted labels hash to a hue.
const LABEL_HUES: Record<string, number> = {
  person: 205, car: 30, truck: 45, bus: 45, motorcycle: 15, bicycle: 160,
  dog: 290, cat: 290, bird: 120, license_plate: 60, plate: 60, face: 330,
}
function hueFor(label: string): number {
  if (label in LABEL_HUES) return LABEL_HUES[label]
  let h = 0
  for (let i = 0; i < label.length; i++) h = (h * 31 + label.charCodeAt(i)) % 360
  return h
}

export function DetectionOverlay({ cameraId, staleMs = 2000 }: DetectionOverlayProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const frameRef = useRef<OverlayFrame | null>(null)
  const rafRef = useRef<number | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

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

    const draw = () => {
      rafRef.current = null
      fit()
      const W = canvas.width, H = canvas.height
      ctx.clearRect(0, 0, W, H)
      const frame = frameRef.current
      if (!frame || Date.now() - frame.receivedAt > staleMs) return

      const dpr = window.devicePixelRatio || 1
      const lw = Math.max(1.5, 2 * dpr)
      const fontPx = Math.max(10, Math.round(12 * dpr))
      ctx.font = `600 ${fontPx}px system-ui, -apple-system, sans-serif`
      ctx.textBaseline = 'top'
      ctx.lineJoin = 'round'

      for (const t of frame.tracks) drawTrack(ctx, t, W, H, lw, fontPx, frame.calibrating)
    }
    const requestDraw = () => { if (rafRef.current == null) rafRef.current = requestAnimationFrame(draw) }

    const unsubscribe = subscribeDetections(cameraId, (frame) => {
      frameRef.current = frame
      requestDraw()
    })
    // Expire stale boxes even when no new frame arrives to trigger a draw.
    const sweep = window.setInterval(() => {
      const f = frameRef.current
      if (f && Date.now() - f.receivedAt > staleMs) { frameRef.current = null; requestDraw() }
    }, 500)

    return () => {
      unsubscribe()
      window.clearInterval(sweep)
      ro.disconnect()
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current)
      ctx.clearRect(0, 0, canvas.width, canvas.height)
    }
  }, [cameraId, staleMs])

  return (
    <canvas
      ref={canvasRef}
      className="absolute inset-0 w-full h-full pointer-events-none"
      aria-hidden="true"
      data-testid="detection-overlay"
    />
  )
}

function drawTrack(
  ctx: CanvasRenderingContext2D, t: OverlayTrack,
  W: number, H: number, lw: number, fontPx: number, calibrating: boolean,
) {
  const [nx, ny, nw, nh] = t.box
  const x = nx * W, y = ny * H, w = nw * W, h = nh * H
  if (!(w > 0 && h > 0)) return
  const hue = hueFor(t.label)
  // Calibrating = the tracker is still settling; draw it, but say so.
  const alpha = calibrating ? 0.55 : 1

  // Box: a dark halo under the coloured stroke keeps it legible on both
  // bright sky and dark tarmac, the two backgrounds a camera sees most.
  ctx.lineWidth = lw + 2
  ctx.strokeStyle = `rgba(0,0,0,${0.55 * alpha})`
  ctx.strokeRect(x, y, w, h)
  ctx.lineWidth = lw
  ctx.strokeStyle = `hsla(${hue}, 85%, 60%, ${alpha})`
  ctx.strokeRect(x, y, w, h)

  // Label chip: "person 91%" (+ id when the tracker has one). Sits above
  // the box, or inside its top edge when the box touches the frame top.
  const idPart = t.id != null ? ` #${t.id}` : ''
  const text = `${t.label}${idPart} ${Math.round(t.score * 100)}%`
  const padX = Math.round(fontPx * 0.45), padY = Math.round(fontPx * 0.25)
  const tw = ctx.measureText(text).width
  const chipW = tw + padX * 2, chipH = fontPx + padY * 2
  const chipX = Math.min(Math.max(0, x), W - chipW)
  const above = y - chipH >= 0
  const chipY = above ? y - chipH : y
  ctx.fillStyle = `hsla(${hue}, 85%, 45%, ${0.92 * alpha})`
  ctx.fillRect(chipX, chipY, chipW, chipH)
  ctx.fillStyle = `rgba(255,255,255,${alpha})`
  ctx.fillText(text, chipX + padX, chipY + padY)
}
