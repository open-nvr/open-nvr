/**
 * Copyright (c) 2026 OpenNVR
 * This file is part of OpenNVR.
 *
 * OpenNVR is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * OpenNVR is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useEdgeAutoPan } from '../hooks/useEdgeAutoPan'

export interface TimelineSegment {
  /** epoch ms */
  startMs: number
  /** epoch ms */
  endMs: number
}

interface PlaybackTimelineProps {
  /** Footage blocks (red). Everything else in the view is a grey gap. */
  segments: TimelineSegment[]
  /** Epoch ms where the still-recording file begins. Footage at/after this is
   *  rendered as a LIVE zone (green) — it is not reliably playable as VOD. */
  liveEdgeMs?: number | null
  /** Visible window (epoch ms). */
  viewStart: number
  viewEnd: number
  /** Playhead wall-clock position (epoch ms). */
  currentTime: number
  /** Fired once per interaction (click, or drag-release) with the target ms. */
  onSeek: (ms: number) => void
  /** Live preview ms while dragging; null on release / mouse-leave. */
  onScrubPreview?: (ms: number | null) => void
  /** Wheel zoom: `anchorMs` is the time under the cursor (kept fixed), `factor`
   *  < 1 zooms in (shrinks the visible span), > 1 zooms out. */
  onZoomAt?: (anchorMs: number, factor: number) => void
  /** Pan the visible window by deltaMs (parent clamps to the day). Enables
   *  edge auto-scroll while scrubbing or painting a clip selection. */
  onPan?: (deltaMs: number) => void
  /** 'seek' (default): drag scrubs. 'clip': drag paints an export selection. */
  mode?: 'seek' | 'clip'
  /** Current clip selection (epoch ms), shown as a highlighted band. */
  selection?: { inMs: number; outMs: number } | null
  /** Fired as the selection is painted (null while empty). */
  onSelectionChange?: (sel: { inMs: number; outMs: number } | null) => void
  className?: string
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

/** Pick a "nice" tick interval (ms) aiming for ~10 ticks across the span. */
function pickTickInterval(spanMs: number): number {
  const candidates = [
    1, 5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600,
  ] // seconds
  const target = spanMs / 1000 / 10
  for (const c of candidates) {
    if (c >= target) return c * 1000
  }
  return candidates[candidates.length - 1] * 1000
}

function fmtTick(ms: number, intervalMs: number): string {
  return new Date(ms).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    ...(intervalMs < 60000 ? { second: '2-digit' } : {}),
    hour12: false,
  })
}

function fmtFull(ms: number): string {
  return new Date(ms).toLocaleString(undefined, {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

export function PlaybackTimeline({
  segments,
  liveEdgeMs = null,
  viewStart,
  viewEnd,
  currentTime,
  onSeek,
  onScrubPreview,
  onZoomAt,
  onPan,
  mode = 'seek',
  selection,
  onSelectionChange,
  className = '',
}: PlaybackTimelineProps) {
  const trackRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const lastXRef = useRef(0)
  const selAnchorRef = useRef<number | null>(null)
  const [dragging, setDragging] = useState(false)
  const [hoverMs, setHoverMs] = useState<number | null>(null)
  const [hoverX, setHoverX] = useState(0)

  const span = Math.max(1, viewEnd - viewStart)
  const toPct = useCallback(
    (ms: number) => clamp(((ms - viewStart) / span) * 100, 0, 100),
    [viewStart, span]
  )

  const xToMs = useCallback(
    (clientX: number): number => {
      const rect = trackRef.current?.getBoundingClientRect()
      if (!rect) return viewStart
      const ratio = clamp((clientX - rect.left) / rect.width, 0, 1)
      return viewStart + ratio * span
    },
    [viewStart, span]
  )

  const inFootage = useCallback(
    (ms: number) => segments.some((s) => ms >= s.startMs && ms < s.endMs),
    [segments]
  )

  /** Snap a click that lands in a gap to the nearest footage edge. */
  const snap = useCallback(
    (ms: number): number => {
      if (inFootage(ms)) return ms
      let best = ms
      let bestDist = Infinity
      for (const s of segments) {
        for (const edge of [s.startMs, s.endMs - 1]) {
          const d = Math.abs(edge - ms)
          if (d < bestDist) {
            bestDist = d
            best = edge
          }
        }
      }
      return best
    },
    [segments, inFootage]
  )

  const blocks = useMemo(
    () =>
      segments
        .filter((s) => s.endMs > viewStart && s.startMs < viewEnd)
        .map((s) => {
          const left = toPct(s.startMs)
          const right = toPct(s.endMs)
          return { left, width: Math.max(0.2, right - left) }
        }),
    [segments, viewStart, viewEnd, toPct]
  )

  // The LIVE zone: footage at/after the still-recording file's start. Drawn
  // over the red blocks so the red/green boundary is the exact live edge.
  const liveBlocks = useMemo(() => {
    if (liveEdgeMs == null) return []
    return segments
      .filter((s) => s.endMs > Math.max(viewStart, liveEdgeMs) && s.startMs < viewEnd)
      .map((s) => {
        const from = Math.max(s.startMs, liveEdgeMs)
        const left = toPct(from)
        const right = toPct(Math.min(s.endMs, viewEnd))
        return { left, width: Math.max(0.2, right - left) }
      })
      .filter((b) => b.width > 0)
  }, [segments, liveEdgeMs, viewStart, viewEnd, toPct])

  const ticks = useMemo(() => {
    const interval = pickTickInterval(span)
    const first = Math.ceil(viewStart / interval) * interval
    const out: { ms: number; pct: number; label: string }[] = []
    for (let t = first; t <= viewEnd; t += interval) {
      out.push({ ms: t, pct: toPct(t), label: fmtTick(t, interval) })
    }
    return out
  }, [span, viewStart, viewEnd, toPct])

  // Wheel zoom — native non-passive listener so we can preventDefault the page
  // scroll. Anchored on the cursor so the time under the pointer stays put.
  useEffect(() => {
    const el = trackRef.current
    if (!el || !onZoomAt) return
    const handler = (e: WheelEvent) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const ratio = clamp((e.clientX - rect.left) / rect.width, 0, 1)
      const anchor = viewStart + ratio * span
      onZoomAt(anchor, e.deltaY < 0 ? 0.8 : 1.25)
    }
    el.addEventListener('wheel', handler, { passive: false })
    return () => el.removeEventListener('wheel', handler)
  }, [onZoomAt, viewStart, span])

  const setHover = (clientX: number) => {
    const rect = wrapRef.current?.getBoundingClientRect()
    setHoverX(clientX - (rect?.left ?? 0))
    setHoverMs(xToMs(clientX))
  }

  const autoPan = useEdgeAutoPan({ rectRef: trackRef, spanMs: span, onPan })

  // Pointer capture keeps the drag alive (and the seek committable) even when
  // the pointer leaves the track — routine while edge auto-panning. Clip drags
  // also feed onScrubPreview so the parent's auto-follow (suppressed while a
  // preview is active) can't recenter the window against the pan.
  const handleDown = (e: React.PointerEvent) => {
    if (!e.isPrimary) return
    e.currentTarget.setPointerCapture(e.pointerId)
    setDragging(true)
    lastXRef.current = e.clientX
    autoPan.onDragStart(e.clientX)
    setHover(e.clientX)
    if (mode === 'clip') {
      const t = xToMs(e.clientX)
      selAnchorRef.current = t
      onSelectionChange?.({ inMs: t, outMs: t })
      onScrubPreview?.(t)
    } else {
      onScrubPreview?.(snap(xToMs(e.clientX)))
    }
  }
  const handleMove = (e: React.PointerEvent) => {
    if (!e.isPrimary) return
    lastXRef.current = e.clientX
    setHover(e.clientX)
    if (!dragging) return
    autoPan.onDragMove(e.clientX)
    if (mode === 'clip' && selAnchorRef.current != null) {
      const t = xToMs(e.clientX)
      const a = selAnchorRef.current
      onSelectionChange?.({ inMs: Math.min(a, t), outMs: Math.max(a, t) })
      onScrubPreview?.(t)
    } else if (mode === 'seek') {
      onScrubPreview?.(snap(xToMs(e.clientX)))
    }
  }
  const handleUp = (e: React.PointerEvent) => {
    if (!dragging) return
    autoPan.onDragEnd()
    setDragging(false)
    if (mode === 'clip') {
      const a = selAnchorRef.current
      selAnchorRef.current = null
      const t = xToMs(e.clientX)
      if (a == null || Math.abs(t - a) < 1000) onSelectionChange?.(null) // a click clears
      else onSelectionChange?.({ inMs: Math.min(a, t), outMs: Math.max(a, t) })
      onScrubPreview?.(null)
    } else {
      onSeek(snap(xToMs(e.clientX)))
      onScrubPreview?.(null)
    }
  }
  // With capture, pointerleave still fires when the pointer exits the bounds —
  // it must only clear the hover chip, never cancel an active drag.
  const handleLeave = () => {
    if (!dragging) setHoverMs(null)
  }
  const handleCancel = () => {
    if (!dragging) return
    autoPan.onDragEnd()
    setDragging(false)
    setHoverMs(null)
    if (mode === 'clip') {
      selAnchorRef.current = null
      onSelectionChange?.(null)
    }
    onScrubPreview?.(null)
  }

  // While auto-panning the pointer holds still, so no pointermove fires —
  // re-derive the drag target from the moving window instead. During a drag
  // the window only moves via onPan (auto-follow is suppressed by the preview).
  useEffect(() => {
    if (!dragging) return
    setHover(lastXRef.current)
    if (mode === 'clip' && selAnchorRef.current != null) {
      const t = xToMs(lastXRef.current)
      const a = selAnchorRef.current
      onSelectionChange?.({ inMs: Math.min(a, t), outMs: Math.max(a, t) })
      onScrubPreview?.(t)
    } else if (mode === 'seek') {
      onScrubPreview?.(snap(xToMs(lastXRef.current)))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewStart, viewEnd])

  const currentPct = toPct(currentTime)
  const currentVisible = currentTime >= viewStart && currentTime <= viewEnd
  const hoverInGap = hoverMs != null && !inFootage(hoverMs)

  return (
    <div ref={wrapRef} className={`relative select-none ${className}`}>
      {/* Playhead time readout — hidden while hovering so it can never
          collide with the hover time chip that shares this strip */}
      <div className="h-5 relative text-[11px] font-mono">
        {currentVisible && hoverMs == null && (
          <span
            className="absolute -translate-x-1/2 px-1 rounded bg-[var(--accent)] text-white whitespace-nowrap"
            style={{ left: `clamp(3.5rem, ${currentPct}%, calc(100% - 3.5rem))` }}
          >
            {fmtFull(currentTime)}
          </span>
        )}
      </div>

      {/* Track */}
      <div
        ref={trackRef}
        className={`relative h-9 bg-neutral-800 overflow-hidden rounded-sm touch-none ${
          mode === 'clip' ? 'cursor-crosshair' : 'cursor-pointer'
        }`}
        onPointerDown={handleDown}
        onPointerMove={handleMove}
        onPointerUp={handleUp}
        onPointerLeave={handleLeave}
        onPointerCancel={handleCancel}
      >
        {/* Footage (red) */}
        {blocks.map((b, i) => (
          <div
            key={i}
            className="absolute top-0 bottom-0"
            style={{ left: `${b.left}%`, width: `${b.width}%`, background: '#dc2626' }}
          />
        ))}

        {/* LIVE zone (green, still recording — plays via Live View) */}
        {liveBlocks.map((b, i) => (
          <div
            key={`live-${i}`}
            className="absolute top-0 bottom-0 flex items-center overflow-hidden"
            style={{ left: `${b.left}%`, width: `${b.width}%`, background: '#16a34a' }}
          >
            {b.width > 4 && (
              <span className="ml-1 flex items-center gap-1 text-[9px] font-semibold text-white/90 whitespace-nowrap">
                <span className="h-1.5 w-1.5 rounded-full bg-white animate-pulse" />
                LIVE
              </span>
            )}
          </div>
        ))}

        {/* Clip selection band */}
        {selection && selection.outMs > selection.inMs && (
          <div
            className="absolute top-0 bottom-0 bg-[var(--accent)]/35 border-x-2 border-[var(--accent)] pointer-events-none"
            style={{
              left: `${toPct(selection.inMs)}%`,
              width: `${toPct(selection.outMs) - toPct(selection.inMs)}%`,
            }}
          />
        )}

        {/* Ticks */}
        {ticks.map((t, i) => (
          <div
            key={i}
            className="absolute top-0 h-2 w-px bg-white/25 pointer-events-none"
            style={{ left: `${t.pct}%` }}
          />
        ))}

        {/* Hover guide */}
        {hoverMs != null && (
          <div
            className="absolute top-0 bottom-0 w-px bg-white/40 pointer-events-none"
            style={{ left: `${toPct(hoverMs)}%` }}
          />
        )}

        {/* Playhead */}
        {currentVisible && (
          <div
            className="absolute top-0 bottom-0 w-0.5 bg-white z-10 pointer-events-none"
            style={{ left: `${currentPct}%` }}
          >
            <div className="absolute top-0 left-1/2 -translate-x-1/2 w-2 h-2 bg-white rotate-45" />
          </div>
        )}
      </div>

      {/* Tick labels */}
      <div className="relative h-4 text-[10px] text-[var(--text-dim)] font-mono">
        {ticks.map((t, i) => (
          <span
            key={i}
            className="absolute -translate-x-1/2 whitespace-nowrap"
            style={{ left: `${t.pct}%` }}
          >
            {t.label}
          </span>
        ))}
      </div>

      {/* Hover tooltip — lives in the readout strip, clamped to the edges */}
      {hoverMs != null && (
        <div
          className="pointer-events-none absolute top-0 z-20 px-1.5 py-0.5 bg-black/90 border border-white/15 text-white text-[10px] font-mono whitespace-nowrap"
          style={{ left: `clamp(3.5rem, ${hoverX}px, calc(100% - 3.5rem))`, transform: 'translate(-50%, -0.15rem)' }}
        >
          {fmtFull(hoverMs)}
          {hoverInGap && <span className="text-amber-400 ml-1">· no recording</span>}
        </div>
      )}
    </div>
  )
}
