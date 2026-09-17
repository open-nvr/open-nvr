/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// Pick a uniform colour off the camera, instead of typing HSV numbers.
//
// The param this replaces asked an operator for two three-number HSV
// bounds. Nobody can produce those: not the showroom manager, not the
// installer, arguably not us without a colour picker open. So the
// operator drags a box over the guard's shirt in a snapshot from the
// actual camera, and the range is measured from the pixels.
//
// Value shape (JSON): {"low": [h, s, v], "high": [h, s, v]} — or, opened
// on one camera from its Set up dialog, a per-camera map of those keyed
// by camera id ({"3": {"low": …, "high": …}}), editing only that entry.
//
// H IS 0-179, NOT 0-360. That is OpenCV's 8-bit hue, which is what the
// app feeds to cv2.inRange, and it is the single easiest thing here to
// get wrong — a 0-360 range silently matches nothing and the guard is
// never recognised, with no error anywhere.

import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiService } from '../../lib/apiService'
import { cameraLabel, usePickedCameraIds } from './CameraPicker'
import { containStyle } from './GeometryEditor'

type Camera = { id: number; name: string }
type Hsv = [number, number, number]
export type HsvRange = { low: Hsv; high: Hsv }

/** How much of the picked patch to keep: trims outliers at both ends. */
const LO_PCT = 5
const HI_PCT = 95
/** Slack added to each bound, so the live feed's noise still matches. */
const PAD: Hsv = [3, 30, 40]

function toRange(v: any): HsvRange | null {
  if (v && Array.isArray(v.low) && Array.isArray(v.high)
      && v.low.length === 3 && v.high.length === 3) {
    return { low: v.low.map(Number) as Hsv, high: v.high.map(Number) as Hsv }
  }
  return null
}

function parseJson(raw: string): any {
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    // A hand-edited value that no longer parses is treated as unset
    // rather than throwing the whole config form away.
    return null
  }
}

/**
 * One pixel, RGB to OpenCV's 8-bit HSV.
 *
 * Hue 0-179 (degrees halved to fit a byte), saturation and value 0-255
 * — the exact convention of cv2.cvtColor(..., COLOR_BGR2HSV) on a uint8
 * image, which is what measures this on the far side.
 */
function rgbToHsv(r: number, g: number, b: number): Hsv {
  const max = Math.max(r, g, b), min = Math.min(r, g, b)
  const d = max - min
  let h = 0
  if (d !== 0) {
    if (max === r) h = 60 * (((g - b) / d) % 6)
    else if (max === g) h = 60 * ((b - r) / d + 2)
    else h = 60 * ((r - g) / d + 4)
  }
  if (h < 0) h += 360
  const s = max === 0 ? 0 : (d / max) * 255
  return [Math.round(h / 2), Math.round(s), Math.round(max)]
}

function percentile(sorted: number[], pct: number): number {
  if (!sorted.length) return 0
  const i = Math.min(sorted.length - 1,
                     Math.max(0, Math.round((pct / 100) * (sorted.length - 1))))
  return sorted[i]
}

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n))

/**
 * A range from the sampled pixels — and whether the hue wraps.
 *
 * Percentiles rather than min/max: a shirt spans light and shadow, and
 * one specular highlight or one dark fold widens a min/max range until
 * it matches the floor as well as the uniform.
 *
 * Red is the awkward colour. Its hue sits at both ends of the scale
 * (~175-179 and ~0-5), and a single low/high cannot express that — the
 * caller is told, because a range that silently matches nothing is
 * worse than being refused.
 */
function rangeFrom(pixels: Hsv[]): { range: HsvRange; hueWraps: boolean } {
  const hs = pixels.map((p) => p[0]).sort((a, b) => a - b)
  const ss = pixels.map((p) => p[1]).sort((a, b) => a - b)
  const vs = pixels.map((p) => p[2]).sort((a, b) => a - b)

  const hLo = percentile(hs, LO_PCT), hHi = percentile(hs, HI_PCT)
  // A hue spread this wide across a patch that is meant to be ONE
  // colour means the samples sit at both ends of the wheel, not that
  // the shirt is a rainbow.
  const hueWraps = hHi - hLo > 90

  return {
    hueWraps,
    range: {
      low: [clamp(hLo - PAD[0], 0, 179),
            clamp(percentile(ss, LO_PCT) - PAD[1], 0, 255),
            clamp(percentile(vs, LO_PCT) - PAD[2], 0, 255)],
      high: [clamp(hHi + PAD[0], 0, 179),
             clamp(percentile(ss, HI_PCT) + PAD[1], 0, 255),
             clamp(percentile(vs, HI_PCT) + PAD[2], 0, 255)],
    },
  }
}

/** cv2.inRange, for one pixel. */
function inRange(px: Hsv, r: HsvRange): boolean {
  return px[0] >= r.low[0] && px[0] <= r.high[0]
    && px[1] >= r.low[1] && px[1] <= r.high[1]
    && px[2] >= r.low[2] && px[2] <= r.high[2]
}

// One camera snapshot as an object URL (revoked on unmount / camera
// change). Same source the geometry editor draws on, so the colour is
// picked off the same picture the zone was drawn on.
function useSnapshotUrl(cameraId: number | string | null) {
  const numericId = typeof cameraId === 'number' ? cameraId : Number(cameraId)
  const enabled = cameraId != null && cameraId !== '' && Number.isFinite(numericId)
  const query = useQuery({
    queryKey: ['camera-snapshot', cameraId],
    queryFn: async () => {
      // The Blob, not an object URL: the cache outlives any one editor.
      // Caching the URL meant a URL one editor had already revoked was
      // handed straight back — deselect a camera, select it again within
      // 30 s, and the snapshot was a broken image. Both editors also
      // share this key, so one closing broke the other's picture.
      const { data } = await apiService.getCameraSnapshot(numericId)
      return data as Blob
    },
    enabled,
    retry: 0,
    staleTime: 30_000,
  })
  // Each caller mints its own URL from the cached Blob and revokes only
  // that one. Kept in state and made in the effect (not a useMemo) so
  // StrictMode's mount → cleanup → mount gets a fresh URL, not a revoked one.
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    const blob = query.data
    if (!blob) { setUrl(null); return }
    const next = URL.createObjectURL(blob)
    setUrl(next)
    return () => URL.revokeObjectURL(next)
  }, [query.data])
  return { data: url, isError: query.isError, isPending: query.isPending }
}

export function ColorRangeEditor({ value, onChange, cameraId, readOnly = false, fit = false }: {
  value: string
  onChange: (json: string) => void
  /** Sample on this camera only, with no selector, editing its entry in a
   *  per-camera map. */
  cameraId?: number | string
  /** Show the colour without allowing a new sample (a camera the user
   *  can't manage). */
  readOnly?: boolean
  /** Fill the parent's height and scale the snapshot to fit it whole. */
  fit?: boolean
}) {
  const fixed = cameraId != null
  const parsed = useMemo(() => parseJson(value), [value])
  const map: Record<string, unknown> = fixed && parsed && typeof parsed === 'object' && !Array.isArray(parsed)
    ? parsed : {}
  const stored = fixed
    ? toRange(map[String(cameraId)] ?? map[`cam${cameraId}`])
    : toRange(parsed)
  /** Write a new range (or clear it), in whichever shape this editor holds. */
  const emit = (range: HsvRange | null) => {
    if (readOnly) return
    if (!fixed) {
      onChange(range ? JSON.stringify(range) : '')
      return
    }
    const next: Record<string, unknown> = { ...map }
    delete next[`cam${cameraId}`]
    if (range) next[String(cameraId)] = range
    else delete next[String(cameraId)]
    onChange(JSON.stringify(next))
  }
  const camerasQuery = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const { data } = await apiService.getCameras()
      const list = (data?.cameras ?? data ?? []) as Camera[]
      return Array.isArray(list) ? list : []
    },
    retry: 0,
  })
  // Inside an app's configuration, sample only from cameras the app uses.
  const pickedIds = usePickedCameraIds()
  const cameras = (camerasQuery.data ?? []).filter(
    (c) => !pickedIds || pickedIds.has(Number(c.id)),
  )
  const nothingPicked = pickedIds !== null && pickedIds.size === 0
  const [chosenCam, setCam] = useState<string>('')
  const cam = fixed ? String(cameraId) : chosenCam
  useEffect(() => {
    if (fixed) return
    if (cam && (!pickedIds || pickedIds.has(Number(cam)))) return
    // Nothing left to offer (the last camera was unpicked) clears the
    // selection, so the editor stops showing that camera's snapshot.
    const first = cameras[0] ? String(cameras[0].id) : ''
    if (first !== cam) setCam(first)
  }, [fixed, cam, cameras, pickedIds])

  const snap = useSnapshotUrl(cam || null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const boxRef = useRef<HTMLDivElement | null>(null)
  // The decoded snapshot, kept so a re-measure never refetches.
  const [pixels, setPixels] = useState<ImageData | null>(null)
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null)
  const [hueWraps, setHueWraps] = useState(false)
  const [match, setMatch] = useState<{ inside: number; outside: number } | null>(null)

  // Decode once per snapshot: getImageData needs the bytes on a canvas,
  // and the object URL is same-origin so the canvas is never tainted.
  const onImageLoad = () => {
    const img = imgRef.current
    if (!img) return
    const c = document.createElement('canvas')
    c.width = img.naturalWidth
    c.height = img.naturalHeight
    const ctx = c.getContext('2d', { willReadFrequently: true })
    if (!ctx) return
    ctx.drawImage(img, 0, 0)
    try {
      setPixels(ctx.getImageData(0, 0, c.width, c.height))
    } catch {
      setPixels(null)   // never fatal; the editor still takes typed values
    }
  }

  /** Measure the drag box, and score the result against the whole frame. */
  const measure = (sel: { x0: number; y0: number; x1: number; y1: number }) => {
    const data = pixels
    if (!data) return
    const X0 = Math.round(Math.min(sel.x0, sel.x1) * data.width)
    const X1 = Math.round(Math.max(sel.x0, sel.x1) * data.width)
    const Y0 = Math.round(Math.min(sel.y0, sel.y1) * data.height)
    const Y1 = Math.round(Math.max(sel.y0, sel.y1) * data.height)
    if (X1 - X0 < 3 || Y1 - Y0 < 3) return

    const picked: Hsv[] = []
    for (let y = Y0; y < Y1; y++) {
      for (let x = X0; x < X1; x++) {
        const i = (y * data.width + x) * 4
        picked.push(rgbToHsv(data.data[i], data.data[i + 1], data.data[i + 2]))
      }
    }
    if (!picked.length) return

    const { range, hueWraps: wraps } = rangeFrom(picked)
    setHueWraps(wraps)

    // How well it separates: matching the shirt is only half the
    // question — a range that also matches the floor is no use, and an
    // operator can see that number and try a different patch.
    let inside = 0
    for (const px of picked) if (inRange(px, range)) inside++
    let outside = 0, seen = 0
    for (let y = 0; y < data.height; y += 3) {
      for (let x = 0; x < data.width; x += 3) {
        if (x >= X0 && x < X1 && y >= Y0 && y < Y1) continue
        const i = (y * data.width + x) * 4
        seen++
        if (inRange(rgbToHsv(data.data[i], data.data[i + 1], data.data[i + 2]), range)) {
          outside++
        }
      }
    }
    setMatch({
      inside: Math.round((100 * inside) / picked.length),
      outside: seen ? Math.round((100 * outside) / seen) : 0,
    })
    emit(range)
  }

  const pointFrom = (e: React.PointerEvent) => {
    const box = boxRef.current
    if (!box) return null
    const r = box.getBoundingClientRect()
    return {
      x: clamp((e.clientX - r.left) / r.width, 0, 1),
      y: clamp((e.clientY - r.top) / r.height, 0, 1),
    }
  }

  const swatch = (hsv: Hsv) =>
    `hsl(${(hsv[0] * 2) % 360} ${Math.round((hsv[1] / 255) * 100)}% ${
      Math.round((hsv[2] / 255) * 100 * 0.6)}%)`

  return (
    <div className={fit ? 'flex h-full min-h-0 flex-col gap-2' : 'space-y-2'}>
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        {fixed ? null : nothingPicked ? (
          <span className="text-xs text-[var(--text-dim)]">Select a camera for this app first — Cameras, above.</span>
        ) : cameras.length === 0 ? (
          <input
            value={cam}
            onChange={(e) => setCam(e.target.value)}
            placeholder="camera id"
            aria-label="Camera id"
            className="w-28 rounded border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-xs"
          />
        ) : (
          <select
            value={cam}
            onChange={(e) => setCam(e.target.value)}
            aria-label="Camera to sample the colour from"
            className="rounded border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-xs"
          >
            {cameras.map((c) => (
              <option key={c.id} value={c.id}>{cameraLabel(c, cameras)}</option>
            ))}
          </select>
        )}
        {stored && (
          <span className="flex items-center gap-1.5 text-xs text-[var(--text-dim)]">
            <span className="inline-block h-4 w-4 rounded-sm border border-[var(--border)]"
                  style={{ background: swatch(stored.low) }} />
            <span className="inline-block h-4 w-4 rounded-sm border border-[var(--border)]"
                  style={{ background: swatch(stored.high) }} />
            H {stored.low[0]}–{stored.high[0]} · S {stored.low[1]}–{stored.high[1]}
            {' '}· V {stored.low[2]}–{stored.high[2]}
          </span>
        )}
        {stored && !readOnly && (
          <button
            type="button"
            onClick={() => { emit(null); setMatch(null); setHueWraps(false) }}
            className="text-xs text-[var(--text-dim)] underline hover:text-[var(--text)]"
          >
            Clear
          </button>
        )}
      </div>

      <div
        className={fit ? 'flex min-h-0 flex-1 items-center justify-center' : ''}
        style={fit ? { containerType: 'size' } : undefined}
      >
      <div
        ref={boxRef}
        className="relative select-none overflow-hidden rounded border border-[var(--border)] bg-[var(--bg-2)]"
        style={{
          ...(fit ? containStyle(16 / 9) : { aspectRatio: '16 / 9' }),
          cursor: pixels && !readOnly ? 'crosshair' : 'default',
        }}
        onPointerDown={(e) => {
          if (!pixels || readOnly) return
          const pt = pointFrom(e)
          if (!pt) return
          ;(e.target as Element).setPointerCapture?.(e.pointerId)
          setDrag({ x0: pt.x, y0: pt.y, x1: pt.x, y1: pt.y })
        }}
        onPointerMove={(e) => {
          if (!drag) return
          const pt = pointFrom(e)
          if (pt) setDrag({ ...drag, x1: pt.x, y1: pt.y })
        }}
        onPointerUp={() => {
          if (drag) measure(drag)
          setDrag(null)
        }}
      >
        {snap.data ? (
          <img
            ref={imgRef}
            src={snap.data}
            alt=""
            onLoad={onImageLoad}
            draggable={false}
            className="h-full w-full object-contain"
          />
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-[var(--text-dim)]">
            {snap.isPending && cam
              ? 'Fetching a snapshot…'
              : 'No snapshot — select a camera that is online to sample its colours.'}
          </div>
        )}
        {drag && (
          <div
            className="pointer-events-none absolute border-2 border-[var(--accent)] bg-[var(--accent)]/20"
            style={{
              left: `${Math.min(drag.x0, drag.x1) * 100}%`,
              top: `${Math.min(drag.y0, drag.y1) * 100}%`,
              width: `${Math.abs(drag.x1 - drag.x0) * 100}%`,
              height: `${Math.abs(drag.y1 - drag.y0) * 100}%`,
            }}
          />
        )}
        <canvas ref={canvasRef} className="hidden" />
      </div>
      </div>

      <div className="shrink-0 text-[11px] text-[var(--text-dim)]">
        {readOnly
          ? "You can't manage this camera, so its colour is shown read-only."
          : <>Drag a box over the guard&apos;s shirt. Pick a patch of the uniform only —
            not the collar, the face or the background.</>}
      </div>

      {match && (
        <div className="shrink-0 text-[11px]">
          <span className="text-[var(--text-dim)]">
            Matches {match.inside}% of what you picked, and {match.outside}% of the
            rest of the picture.
          </span>{' '}
          {match.outside > 15 ? (
            <span style={{ color: 'var(--warn)' }}>
              That is a lot of the room — try a patch that is only uniform, or a
              camera where the guard is better lit.
            </span>
          ) : (
            <span style={{ color: 'var(--ok)' }}>Good separation.</span>
          )}
        </div>
      )}

      {hueWraps && (
        <div className="shrink-0 text-[11px]" style={{ color: 'var(--warn)' }}>
          This colour sits at both ends of the hue scale — reds usually do — and a
          single range cannot express that, so it will match far less than you
          picked. Sample a different garment, or leave this unset and let the scan
          zone identify the guard.
        </div>
      )}
    </div>
  )
}
