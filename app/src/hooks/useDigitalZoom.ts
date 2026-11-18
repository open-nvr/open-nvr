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

/**
 * Digital zoom for a video surface: Ctrl/⌘ + scroll (and a trackpad pinch,
 * which browsers report as Ctrl + wheel) zooms about the cursor, dragging
 * pans while zoomed, two fingers pinch on a touch screen. Nothing is asked
 * of the camera: the picture is scaled with a CSS transform, so it works
 * the same on live video and recordings, and anything drawn in the same
 * layer (detection boxes) stays on the object it marks.
 *
 * The surface element receives the gestures; the layer inside it gets
 * ``layerStyle``. The view is kept so the picture always covers the
 * surface: no panning past an edge, never smaller than 1×.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react'

export const MAX_ZOOM = 8

/** The Ctrl + scroll reminder shows at most this often, page-wide. */
const HINT_EVERY_MS = 20_000
let lastHintAt = 0

type View = { s: number; x: number; y: number }
const IDENTITY: View = { s: 1, x: 0, y: 0 }

function clampView(v: View, w: number, h: number): View {
  const s = Math.min(MAX_ZOOM, Math.max(1, v.s))
  if (s === 1) return IDENTITY
  return {
    s,
    x: Math.min(0, Math.max(w * (1 - s), v.x)),
    y: Math.min(0, Math.max(h * (1 - s), v.y)),
  }
}

export function useDigitalZoom() {
  // A callback ref, so the listeners below follow the element even when it
  // mounts after the hook (a player that starts on an error screen).
  const surfaceRef = useRef<HTMLDivElement | null>(null)
  const [el, setEl] = useState<HTMLDivElement | null>(null)
  const attach = useCallback((node: HTMLDivElement | null) => {
    surfaceRef.current = node
    setEl(node)
  }, [])
  const [view, setViewState] = useState<View>(IDENTITY)
  const viewRef = useRef<View>(IDENTITY)
  const [hint, setHint] = useState(false)
  const hintTimer = useRef<number | undefined>(undefined)
  const pointers = useRef(new Map<number, { x: number; y: number }>())
  const pinchDist = useRef<number | null>(null)
  const dragged = useRef(false)
  const downAt = useRef<{ x: number; y: number } | null>(null)

  const size = () => {
    const el = surfaceRef.current
    return el ? { w: el.clientWidth, h: el.clientHeight } : { w: 0, h: 0 }
  }

  const setView = useCallback((next: View) => {
    const { w, h } = size()
    const v = clampView(next, w, h)
    viewRef.current = v
    setViewState(v)
  }, [])

  /** Zoom by ``factor`` keeping the surface point (px, py) where it is. */
  const zoomAt = useCallback((px: number, py: number, factor: number) => {
    const v = viewRef.current
    const s = Math.min(MAX_ZOOM, Math.max(1, v.s * factor))
    const k = s / v.s
    setView({ s, x: px - (px - v.x) * k, y: py - (py - v.y) * k })
  }, [setView])

  const zoomCentre = useCallback((factor: number) => {
    const { w, h } = size()
    zoomAt(w / 2, h / 2, factor)
  }, [zoomAt])

  const reset = useCallback(() => setView(IDENTITY), [setView])

  // Wheel: a native, non-passive listener, since React's onWheel is passive
  // and could not keep Ctrl+scroll from zooming the whole page.
  useEffect(() => {
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if (!(e.ctrlKey || e.metaKey)) {
        // Plain scrolling stays the page's; now and then, say how zoom works.
        if (viewRef.current.s === 1 && Date.now() - lastHintAt > HINT_EVERY_MS) {
          lastHintAt = Date.now()
          setHint(true)
          window.clearTimeout(hintTimer.current)
          hintTimer.current = window.setTimeout(() => setHint(false), 1500)
        }
        return
      }
      e.preventDefault()
      const r = el.getBoundingClientRect()
      // deltaY is ~100 per mouse notch and a few units per pinch frame;
      // exp() maps both to a smooth, symmetric factor.
      const dy = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY
      zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-dy * 0.002))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => {
      el.removeEventListener('wheel', onWheel)
      window.clearTimeout(hintTimer.current)
    }
  }, [el, zoomAt])

  // A drag that panned is not a click: swallow the click that ends it, so
  // it does not also pause a recording or select a tile.
  useEffect(() => {
    if (!el) return
    const onClick = (e: MouseEvent) => {
      if (dragged.current) {
        dragged.current = false
        e.stopPropagation()
        e.preventDefault()
      }
    }
    el.addEventListener('click', onClick, true)
    return () => el.removeEventListener('click', onClick, true)
  }, [el])

  // Keep the same part of the picture in view when the surface resizes
  // (fullscreen, layout change).
  useEffect(() => {
    if (!el || typeof ResizeObserver === 'undefined') return
    let last = { w: el.clientWidth, h: el.clientHeight }
    const ro = new ResizeObserver(() => {
      const now = { w: el.clientWidth, h: el.clientHeight }
      const v = viewRef.current
      if (v.s > 1 && last.w > 0 && last.h > 0) {
        setView({ s: v.s, x: (v.x * now.w) / last.w, y: (v.y * now.h) / last.h })
      }
      last = now
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [el, setView])

  const local = (e: { clientX: number; clientY: number }) => {
    const r = surfaceRef.current!.getBoundingClientRect()
    return { x: e.clientX - r.left, y: e.clientY - r.top }
  }

  const onPointerDown = (e: ReactPointerEvent<HTMLElement>) => {
    // A new press: whatever the last drag was, it no longer owns a click.
    dragged.current = false
    if (e.pointerType === 'mouse' && e.button !== 0) return
    // Not on the controls drawn over the picture (PTZ pad, buttons).
    if ((e.target as HTMLElement).closest('button, input, select, [role=group], [data-no-zoom-pan]')) return
    pointers.current.set(e.pointerId, local(e))
    const zoomed = viewRef.current.s > 1
    if (zoomed || pointers.current.size > 1) {
      // Ours: keep the tile from starting a drag-to-rearrange.
      e.stopPropagation()
      e.currentTarget.setPointerCapture(e.pointerId)
    }
    if (pointers.current.size === 1) downAt.current = local(e)
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()]
      pinchDist.current = Math.hypot(a.x - b.x, a.y - b.y)
    }
  }

  const onPointerMove = (e: ReactPointerEvent<HTMLElement>) => {
    const prev = pointers.current.get(e.pointerId)
    if (!prev) return
    const p = local(e)
    pointers.current.set(e.pointerId, p)
    if (pointers.current.size === 2 && pinchDist.current) {
      const [a, b] = [...pointers.current.values()]
      const d = Math.hypot(a.x - b.x, a.y - b.y)
      if (d > 0) zoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, d / pinchDist.current)
      pinchDist.current = d
      dragged.current = true
      return
    }
    const v = viewRef.current
    if (v.s <= 1) return
    if (downAt.current && Math.hypot(p.x - downAt.current.x, p.y - downAt.current.y) > 3) {
      dragged.current = true
    }
    setView({ s: v.s, x: v.x + (p.x - prev.x), y: v.y + (p.y - prev.y) })
  }

  const onPointerEnd = (e: ReactPointerEvent<HTMLElement>) => {
    pointers.current.delete(e.pointerId)
    // The click a drag ends with (if any) fires right after pointerup; past
    // that, nothing is left to swallow.
    if (dragged.current) window.setTimeout(() => { dragged.current = false }, 0)
    if (pointers.current.size < 2) pinchDist.current = null
    if (pointers.current.size === 0) downAt.current = null
  }

  const zoomed = view.s > 1
  const layerStyle: CSSProperties = zoomed
    ? { transform: `translate(${view.x}px, ${view.y}px) scale(${view.s})`, transformOrigin: '0 0' }
    : {}
  const surfaceProps = {
    ref: attach,
    onPointerDown,
    onPointerMove,
    onPointerUp: onPointerEnd,
    onPointerCancel: onPointerEnd,
    style: {
      // Zoomed, one finger pans the picture instead of the page.
      touchAction: zoomed ? 'none' : 'pan-x pan-y',
      cursor: zoomed ? 'grab' : undefined,
    } as CSSProperties,
  }

  return { surfaceProps, layerStyle, view, scale: view.s, zoomed, zoomCentre, reset, hint }
}
