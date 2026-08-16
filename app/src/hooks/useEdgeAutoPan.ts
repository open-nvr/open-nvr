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

import { useCallback, useEffect, useRef, type RefObject } from 'react'

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

export interface EdgeAutoPanOptions {
  /** Track/overlay element whose bounds define the edge zones. */
  rectRef: RefObject<HTMLElement | null>
  /** Visible window span (ms) — pan speed scales with it so the feel is
   *  zoom-independent. */
  spanMs: number
  /** Pan the window by deltaMs (the parent clamps to its bounds). Absent
   *  disables the feature entirely. */
  onPan?: (deltaMs: number) => void
  /** Width of the auto-pan zone at each edge. */
  edgeZonePx?: number
  /** Max pan speed, as a fraction of the visible span per second. */
  maxWindowFractionPerSec?: number
  /** Pointer travel required before a drag can pan — keeps a plain click
   *  near the edge from nudging the window. */
  dragThresholdPx?: number
}

/**
 * Auto-pans a timeline window while a scrub drag holds the pointer inside an
 * edge zone (or beyond the element — pointer capture keeps the drag alive).
 * Runs one rAF loop per drag; frames outside the zones are no-ops. Speed is
 * proportional to how deep into the zone the pointer sits.
 */
export function useEdgeAutoPan(options: EdgeAutoPanOptions): {
  onDragStart: (clientX: number) => void
  onDragMove: (clientX: number) => void
  onDragEnd: () => void
} {
  // The rAF closure reads through a ref so it never goes stale mid-drag.
  const optsRef = useRef(options)
  optsRef.current = options

  const lastXRef = useRef(0)
  const originXRef = useRef(0)
  const movedRef = useRef(false)
  const rafRef = useRef<number | null>(null)
  const lastTsRef = useRef(0)

  const onDragEnd = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = null
    }
  }, [])

  useEffect(() => onDragEnd, [onDragEnd])

  const frame = useCallback((ts: number) => {
    rafRef.current = requestAnimationFrame(frame)
    // Cap dt so resuming from a background tab can't teleport the window.
    const dt = Math.min(100, ts - lastTsRef.current)
    lastTsRef.current = ts
    const { rectRef, spanMs, onPan, edgeZonePx = 32, maxWindowFractionPerSec = 0.6 } = optsRef.current
    if (!onPan || !movedRef.current || dt <= 0) return
    const rect = rectRef.current?.getBoundingClientRect()
    if (!rect || rect.width <= 0) return
    // Depth 0..1 into each zone; a pointer beyond the element clamps to full
    // speed. Signed sum handles zones overlapping on very narrow tracks.
    const x = lastXRef.current
    const leftDepth = clamp((rect.left + edgeZonePx - x) / edgeZonePx, 0, 1)
    const rightDepth = clamp((x - (rect.right - edgeZonePx)) / edgeZonePx, 0, 1)
    const depth = rightDepth - leftDepth
    if (depth === 0) return
    onPan(depth * spanMs * maxWindowFractionPerSec * (dt / 1000))
  }, [])

  const onDragStart = useCallback(
    (clientX: number) => {
      onDragEnd()
      lastXRef.current = clientX
      originXRef.current = clientX
      movedRef.current = false
      lastTsRef.current = performance.now()
      rafRef.current = requestAnimationFrame(frame)
    },
    [frame, onDragEnd]
  )

  const onDragMove = useCallback((clientX: number) => {
    lastXRef.current = clientX
    const { dragThresholdPx = 4 } = optsRef.current
    if (Math.abs(clientX - originXRef.current) >= dragThresholdPx) movedRef.current = true
  }, [])

  return { onDragStart, onDragMove, onDragEnd }
}
