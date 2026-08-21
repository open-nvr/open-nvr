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

import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { TimelineSegment } from './PlaybackTimeline'
import { useEdgeAutoPan } from '../hooks/useEdgeAutoPan'

export interface TimelineRow {
  id: number
  name: string
  segments: TimelineSegment[]
  liveEdgeMs: number | null
}

interface MultiCamTimelineProps {
  rows: TimelineRow[]
  /** Visible window (epoch ms), shared by every row. */
  viewStart: number
  viewEnd: number
  /** Playhead wall-clock position (epoch ms) — one vertical line across rows. */
  currentTime: number
  onSeek: (ms: number) => void
  /** Live preview ms while dragging; null on release / mouse-leave. */
  onScrubPreview?: (ms: number | null) => void
  /** Wheel zoom anchored at the cursor. factor < 1 zooms in. */
  onZoomAt?: (anchorMs: number, factor: number) => void
  /** Pan the visible window by deltaMs (parent clamps to the day). Enables
   *  edge auto-scroll while scrubbing. */
  onPan?: (deltaMs: number) => void
  /** Camera id whose row label is highlighted (audio/selection focus). */
  activeId?: number | null
  onRowClick?: (id: number) => void
  className?: string
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

/** Pick a "nice" tick interval (ms) aiming for ~10 ticks across the span. */
function pickTickInterval(spanMs: number): number {
  const candidates = [1, 5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600] // s
  const target = spanMs / 1000 / 10
  for (const c of candidates) if (c >= target) return c * 1000
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

/**
 * One camera's track row. Memoized: the parent re-renders at 4Hz for the
 * playhead, but a row's segment blocks only change on zoom/pan/data change —
 * with stable props (react-query keeps unchanged segment arrays referentially
 * identical), the memo skips all of that DOM work on every clock tick.
 */
const TrackRow = memo(function TrackRow({
  row,
  viewStart,
  viewEnd,
  rowH,
  active,
  onRowClick,
}: {
  row: TimelineRow
  viewStart: number
  viewEnd: number
  rowH: string
  active: boolean
  onRowClick?: (id: number) => void
}) {
  const span = Math.max(1, viewEnd - viewStart)
  const toPct = (ms: number) => clamp(((ms - viewStart) / span) * 100, 0, 100)

  const blocks = useMemo(
    () =>
      row.segments
        .filter((s) => s.endMs > viewStart && s.startMs < viewEnd)
        .map((s) => {
          const left = toPct(s.startMs)
          return { left, width: Math.max(0.15, toPct(s.endMs) - left) }
        }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [row.segments, viewStart, viewEnd]
  )

  const liveBlocks = useMemo(() => {
    if (row.liveEdgeMs == null) return []
    return row.segments
      .filter((s) => s.endMs > Math.max(viewStart, row.liveEdgeMs!) && s.startMs < viewEnd)
      .map((s) => {
        const from = Math.max(s.startMs, row.liveEdgeMs!)
        const left = toPct(from)
        return { left, width: Math.max(0.15, toPct(Math.min(s.endMs, viewEnd)) - left) }
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row.segments, row.liveEdgeMs, viewStart, viewEnd])

  return (
    <div className="flex items-center">
      <button
        onClick={() => onRowClick?.(row.id)}
        title={row.name}
        className={`w-16 sm:w-28 shrink-0 pr-2 text-right text-[10px] truncate leading-none ${
          active
            ? 'text-[var(--accent)] font-semibold'
            : 'text-[var(--text-dim)] hover:text-[var(--text)]'
        }`}
      >
        {row.name}
      </button>
      <div className={`flex-1 relative ${rowH} bg-neutral-800 overflow-hidden`}>
        {blocks.map((b, i) => (
          <div
            key={i}
            className="absolute top-0 bottom-0"
            style={{ left: `${b.left}%`, width: `${b.width}%`, background: '#dc2626' }}
          />
        ))}
        {liveBlocks.map((b, i) => (
          <div
            key={`live-${i}`}
            className="absolute top-0 bottom-0"
            style={{ left: `${b.left}%`, width: `${b.width}%`, background: '#16a34a' }}
          />
        ))}
      </div>
    </div>
  )
})

/**
 * CP-Plus-style multi-camera timeline: one shared time axis, one track row per
 * camera (red = footage, green = still-recording live zone), and a single
 * playhead spanning every row. Click/drag anywhere in the track area to seek
 * all cameras to that wall-clock instant; wheel to zoom around the cursor.
 */
export function MultiCamTimeline({
  rows,
  viewStart,
  viewEnd,
  currentTime,
  onSeek,
  onScrubPreview,
  onZoomAt,
  onPan,
  activeId = null,
  onRowClick,
  className = '',
}: MultiCamTimelineProps) {
  const overlayRef = useRef<HTMLDivElement>(null)
  const lastXRef = useRef(0)
  const [dragging, setDragging] = useState(false)
  const [hoverMs, setHoverMs] = useState<number | null>(null)

  const span = Math.max(1, viewEnd - viewStart)
  const toPct = useCallback(
    (ms: number) => clamp(((ms - viewStart) / span) * 100, 0, 100),
    [viewStart, span]
  )

  const xToMs = useCallback(
    (clientX: number): number => {
      const rect = overlayRef.current?.getBoundingClientRect()
      if (!rect) return viewStart
      const ratio = clamp((clientX - rect.left) / rect.width, 0, 1)
      return viewStart + ratio * span
    },
    [viewStart, span]
  )

  // Union of all rows' footage — a global gap (no camera has footage) snaps
  // the seek to the nearest recorded edge so clicks always land on video.
  const unionSegs = useMemo(() => rows.flatMap((r) => r.segments), [rows])
  const inAnyFootage = useCallback(
    (ms: number) => unionSegs.some((s) => ms >= s.startMs && ms < s.endMs),
    [unionSegs]
  )
  const snap = useCallback(
    (ms: number): number => {
      if (inAnyFootage(ms)) return ms
      let best = ms
      let bestDist = Infinity
      for (const s of unionSegs) {
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
    [unionSegs, inAnyFootage]
  )

  const ticks = useMemo(() => {
    const interval = pickTickInterval(span)
    const first = Math.ceil(viewStart / interval) * interval
    const out: { pct: number; label: string }[] = []
    for (let t = first; t <= viewEnd; t += interval) {
      out.push({ pct: toPct(t), label: fmtTick(t, interval) })
    }
    return out
  }, [span, viewStart, viewEnd, toPct])

  // Wheel zoom — native non-passive listener so we can preventDefault the
  // page scroll. Anchored on the cursor so the time under it stays put.
  useEffect(() => {
    const el = overlayRef.current
    if (!el || !onZoomAt) return
    const handler = (e: WheelEvent) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const ratio = clamp((e.clientX - rect.left) / rect.width, 0, 1)
      onZoomAt(viewStart + ratio * span, e.deltaY < 0 ? 0.8 : 1.25)
    }
    el.addEventListener('wheel', handler, { passive: false })
    return () => el.removeEventListener('wheel', handler)
  }, [onZoomAt, viewStart, span])

  const autoPan = useEdgeAutoPan({ rectRef: overlayRef, spanMs: span, onPan })

  // Pointer capture keeps the drag alive (and the seek committable) even when
  // the pointer leaves the overlay — routine while edge auto-panning.
  const handleDown = (e: React.PointerEvent) => {
    if (!e.isPrimary) return
    e.currentTarget.setPointerCapture(e.pointerId)
    setDragging(true)
    lastXRef.current = e.clientX
    autoPan.onDragStart(e.clientX)
    setHoverMs(xToMs(e.clientX))
    onScrubPreview?.(snap(xToMs(e.clientX)))
  }
  const handleMove = (e: React.PointerEvent) => {
    if (!e.isPrimary) return
    lastXRef.current = e.clientX
    setHoverMs(xToMs(e.clientX))
    if (dragging) {
      autoPan.onDragMove(e.clientX)
      onScrubPreview?.(snap(xToMs(e.clientX)))
    }
  }
  const handleUp = (e: React.PointerEvent) => {
    if (!dragging) return
    autoPan.onDragEnd()
    setDragging(false)
    onSeek(snap(xToMs(e.clientX)))
    onScrubPreview?.(null)
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
    onScrubPreview?.(null)
  }

  // While auto-panning the pointer holds still, so no pointermove fires —
  // re-derive the preview from the moving window instead. During a drag the
  // window only moves via onPan (auto-follow is suppressed by the preview).
  useEffect(() => {
    if (!dragging) return
    const ms = xToMs(lastXRef.current)
    setHoverMs(ms)
    onScrubPreview?.(snap(ms))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewStart, viewEnd])

  const currentPct = toPct(currentTime)
  const currentVisible = currentTime >= viewStart && currentTime <= viewEnd
  const hoverInGap = hoverMs != null && !inAnyFootage(hoverMs)
  // Thin CP-Plus-style tracks; the shared overlay keeps them easy to click.
  const rowH = rows.length > 2 ? 'h-2.5' : 'h-3'

  return (
    <div className={`select-none ${className}`}>
      {/* Readout strip above the tracks: playhead chip normally, hover chip
          while pointing. Only one shows at a time so they can never collide,
          and neither ever covers the tracks or tick labels. */}
      <div className="flex">
        <div className="w-16 sm:w-28 shrink-0" />
        <div className="flex-1 h-4 relative text-[10px] font-mono">
          {currentVisible && hoverMs == null && (
            <span
              className="absolute -translate-x-1/2 px-1 bg-[var(--accent)] text-white whitespace-nowrap z-10"
              style={{ left: `clamp(3.5rem, ${currentPct}%, calc(100% - 3.5rem))` }}
            >
              {fmtFull(currentTime)}
            </span>
          )}
          {hoverMs != null && (
            <span
              className="pointer-events-none absolute -translate-x-1/2 z-20 px-1.5 bg-black/90 border border-white/15 text-white whitespace-nowrap"
              style={{ left: `clamp(3.5rem, ${toPct(hoverMs)}%, calc(100% - 3.5rem))` }}
            >
              {fmtFull(hoverMs)}
              {hoverInGap && <span className="text-amber-400 ml-1">· no recording</span>}
            </span>
          )}
        </div>
      </div>

      {/* Label column + track rows, with one interactive overlay across rows */}
      <div className="relative">
        <div className="space-y-0.5">
          {rows.map((row) => (
            <TrackRow
              key={row.id}
              row={row}
              viewStart={viewStart}
              viewEnd={viewEnd}
              rowH={rowH}
              active={row.id === activeId}
              onRowClick={onRowClick}
            />
          ))}

          {/* Tick marks row */}
          <div className="flex">
            <div className="w-16 sm:w-28 shrink-0" />
            <div className="flex-1 relative h-3.5 text-[9px] text-[var(--text-dim)] font-mono">
              {ticks.map((t, i) => (
                <span key={i} className="absolute -translate-x-1/2 whitespace-nowrap" style={{ left: `${t.pct}%` }}>
                  {t.label}
                </span>
              ))}
            </div>
          </div>
        </div>

        {/* Interactive overlay spanning every track row (not the labels) */}
        <div
          ref={overlayRef}
          className="absolute inset-y-0 left-16 sm:left-28 right-0 cursor-pointer touch-none"
          onPointerDown={handleDown}
          onPointerMove={handleMove}
          onPointerUp={handleUp}
          onPointerLeave={handleLeave}
          onPointerCancel={handleCancel}
        >
          {/* Tick guides */}
          {ticks.map((t, i) => (
            <div
              key={i}
              className="absolute top-0 bottom-3.5 w-px bg-white/10 pointer-events-none"
              style={{ left: `${t.pct}%` }}
            />
          ))}

          {/* Hover guide (time chip lives in the readout strip above) */}
          {hoverMs != null && (
            <div
              className="absolute top-0 bottom-3.5 w-px bg-white/40 pointer-events-none"
              style={{ left: `${toPct(hoverMs)}%` }}
            />
          )}

          {/* Playhead spanning all rows */}
          {currentVisible && (
            <div
              className="absolute top-0 bottom-3.5 w-0.5 bg-white z-10 pointer-events-none"
              style={{ left: `${currentPct}%` }}
            >
              <div className="absolute top-0 left-1/2 -translate-x-1/2 w-2 h-2 bg-white rotate-45" />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
