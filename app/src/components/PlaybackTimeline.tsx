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

export interface TimelineSegment {
  /** epoch ms */
  startMs: number
  /** epoch ms */
  endMs: number
}

interface PlaybackTimelineProps {
  /** Footage blocks (red). Everything else in the view is a grey gap. */
  segments: TimelineSegment[]
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
  viewStart,
  viewEnd,
  currentTime,
  onSeek,
  onScrubPreview,
  onZoomAt,
  mode = 'seek',
  selection,
  onSelectionChange,
  className = '',
}: PlaybackTimelineProps) {
  const trackRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
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

  const handleDown = (e: React.MouseEvent) => {
    setDragging(true)
    setHover(e.clientX)
    if (mode === 'clip') {
      const t = xToMs(e.clientX)
      selAnchorRef.current = t
      onSelectionChange?.({ inMs: t, outMs: t })
    } else {
      onScrubPreview?.(snap(xToMs(e.clientX)))
    }
  }
  const handleMove = (e: React.MouseEvent) => {
    setHover(e.clientX)
    if (!dragging) return
    if (mode === 'clip' && selAnchorRef.current != null) {
      const t = xToMs(e.clientX)
      const a = selAnchorRef.current
      onSelectionChange?.({ inMs: Math.min(a, t), outMs: Math.max(a, t) })
    } else if (mode === 'seek') {
      onScrubPreview?.(snap(xToMs(e.clientX)))
    }
  }
  const handleUp = (e: React.MouseEvent) => {
    if (!dragging) return
    setDragging(false)
    if (mode === 'clip') {
      const a = selAnchorRef.current
      selAnchorRef.current = null
      const t = xToMs(e.clientX)
      if (a == null || Math.abs(t - a) < 1000) onSelectionChange?.(null) // a click clears
      else onSelectionChange?.({ inMs: Math.min(a, t), outMs: Math.max(a, t) })
    } else {
      onSeek(snap(xToMs(e.clientX)))
      onScrubPreview?.(null)
    }
  }
  const handleLeave = () => {
    setHoverMs(null)
    if (dragging) {
      setDragging(false)
      if (mode === 'seek') onScrubPreview?.(null)
    }
  }

  const currentPct = toPct(currentTime)
  const currentVisible = currentTime >= viewStart && currentTime <= viewEnd
  const hoverInGap = hoverMs != null && !inFootage(hoverMs)

  return (
    <div ref={wrapRef} className={`relative select-none ${className}`}>
      {/* Playhead time readout */}
      <div className="h-5 relative text-[11px] font-mono">
        {currentVisible && (
          <span
            className="absolute -translate-x-1/2 px-1 rounded bg-[var(--accent)] text-white whitespace-nowrap"
            style={{ left: `${currentPct}%` }}
          >
            {fmtFull(currentTime)}
          </span>
        )}
      </div>

      {/* Track */}
      <div
        ref={trackRef}
        className={`relative h-9 bg-neutral-800 overflow-hidden rounded-sm ${
          mode === 'clip' ? 'cursor-crosshair' : 'cursor-pointer'
        }`}
        onMouseDown={handleDown}
        onMouseMove={handleMove}
        onMouseUp={handleUp}
        onMouseLeave={handleLeave}
      >
        {/* Footage (red) */}
        {blocks.map((b, i) => (
          <div
            key={i}
            className="absolute top-0 bottom-0"
            style={{ left: `${b.left}%`, width: `${b.width}%`, background: '#dc2626' }}
          />
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

      {/* Hover tooltip */}
      {hoverMs != null && (
        <div
          className="pointer-events-none absolute top-0 z-20 px-1.5 py-0.5 rounded bg-black/85 text-white text-[10px] font-mono whitespace-nowrap"
          style={{ left: hoverX, transform: 'translate(-50%, -1.2rem)' }}
        >
          {fmtFull(hoverMs)}
          {hoverInGap && <span className="text-neutral-400 ml-1">· no recording</span>}
        </div>
      )}
    </div>
  )
}
