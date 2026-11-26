/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// One of an app's cameras, live, beside the figures about it.
//
// The rest of an app page is history and counters: what was screened,
// what went missing, who was flagged. This is the one panel that answers
// "what is happening there now" — and with the app's own boxes drawn on
// it, it also shows the app's logic working, which is the difference
// between trusting a figure and merely reading one. Used by the guard
// compliance page and by every app's own page (views/AppView.tsx).
//
// Lifted from LiveView's tile (views/LiveView.tsx), minus the drag,
// PTZ and camera-assignment machinery a dashboard panel has no use for.
// Three things carried over deliberately, because each one is a bug
// waiting to happen on a page that polls:
//
//  * the callbacks are held in REFS. VideoPlayer re-runs its WebRTC
//    setup when the identity of onError/onAuthExpired changes, and this
//    page re-renders on a 15s and a 30s poll — an inline arrow here
//    would tear the stream down and rebuild it twice a minute.
//  * stream tokens last an hour. A wall-mounted dashboard outlives
//    that, so an expiry has to refetch rather than die quietly.
//  * the player is keyed on camera + version, so a recovered camera
//    remounts against a fresh token instead of retrying a stale one.

import { useCallback, useEffect, useRef, useState } from 'react'
import { CameraOff, Layers, Minimize2, PictureInPicture2, Settings2, VideoOff } from 'lucide-react'
import { useClickOutside } from '../../hooks/useClickOutside'
import { hiddenLayersKey, type OverlayLayer } from '../../lib/overlayLayers'
import { apiService } from '../../lib/apiService'
import { rebaseToCurrentOrigin } from '../../lib/streamUrl'
import { useCameraStatus } from '../../hooks/useCameraStatus'
import { VideoPlayer } from '../VideoPlayer'
import { Card, CardContent } from '../ui'

type Urls = { whep?: string; hls?: string; token?: string }

/** Where the floating panel was last left. */
const POS_KEY = 'opennvr.guardscan.livePos'
/** …and how big it was made. */
const SIZE_KEY = 'opennvr.liveCameraPanel.popSize'
const SIZE_DEFAULT = { w: 420, h: 300 }
//: Small enough to tuck in a corner, never so small the picture is lost.
const SIZE_MIN = { w: 280, h: 190 }

type Corner = 'nw' | 'ne' | 'sw' | 'se'
const CORNERS: { corner: Corner; place: string; cursor: string; path: string }[] = [
  { corner: 'nw', place: 'left-0 top-0', cursor: 'cursor-nwse-resize', path: 'M2 10 L10 2 M2 14 L14 2' },
  { corner: 'ne', place: 'right-0 top-0', cursor: 'cursor-nesw-resize', path: 'M6 2 L14 10 M2 2 L14 14' },
  { corner: 'sw', place: 'left-0 bottom-0', cursor: 'cursor-nesw-resize', path: 'M2 6 L10 14 M2 2 L14 14' },
  { corner: 'se', place: 'right-0 bottom-0', cursor: 'cursor-nwse-resize', path: 'M14 6 L6 14 M14 2 L2 14' },
]

export function LiveCameraPanel({
  cameraId, cameraName, cameras = [], onSelectCamera, emptyMessage, onPickCameras,
  overlayEnabled, popped = false, onTogglePop,
}: {
  cameraId: number | null
  cameraName: string
  /** The cameras this panel may show — the app's picked cameras. With
   *  more than one, the title becomes a switcher. */
  cameras?: { id: number; name: string }[]
  onSelectCamera?: (id: number) => void
  /** Why there is nothing to show (e.g. no camera picked for the app),
   *  in place of the generic prompt. */
  emptyMessage?: string
  /** Offered beside the empty message: opens the app's camera picks. */
  onPickCameras?: () => void
  /** Whether an admin has let this app draw on live video. When it is
   *  off the picture is fine and the boxes simply never come, which
   *  looks broken unless somebody says so. */
  overlayEnabled?: boolean
  /** Floating free of the column, draggable anywhere on the page. */
  popped?: boolean
  onTogglePop?: () => void
}) {
  // Where the floating panel sits. Only read while popped, and kept
  // here rather than in the page so docking always returns it to the
  // column cleanly.
  const [pos, setPos] = useState(() => {
    try {
      const raw = window.localStorage.getItem(POS_KEY)
      const v = raw ? JSON.parse(raw) : null
      return v && typeof v.x === 'number' && typeof v.y === 'number'
        ? (v as { x: number; y: number })
        : { x: 0, y: 0 }
    } catch {
      return { x: 0, y: 0 }
    }
  })
  const dragFrom = useRef<{ x: number; y: number } | null>(null)

  // How big the floating panel is. A grip on every corner; each keeps
  // the OPPOSITE corner where it is, as window managers do.
  const [size, setSize] = useState(() => {
    try {
      const v = JSON.parse(window.localStorage.getItem(SIZE_KEY) || 'null')
      return v && typeof v.w === 'number' && typeof v.h === 'number'
        ? { w: Math.max(SIZE_MIN.w, v.w), h: Math.max(SIZE_MIN.h, v.h) }
        : SIZE_DEFAULT
    } catch {
      return SIZE_DEFAULT
    }
  })
  const startResize = (corner: Corner) => (down: React.PointerEvent) => {
    down.preventDefault()
    down.stopPropagation()
    const grip = down.currentTarget as HTMLElement
    grip.setPointerCapture(down.pointerId)
    const from = { x: down.clientX, y: down.clientY, w: size.w, h: size.h, px: pos.x, py: pos.y }
    // Growing to the left or up moves only the far edges; the panel is
    // placed by its right and bottom offsets, so growing to the right or
    // down also shifts those by the same amount.
    const east = corner === 'ne' || corner === 'se'
    const south = corner === 'sw' || corner === 'se'
    let at = { w: from.w, h: from.h, x: from.px, y: from.py }
    const move = (e: PointerEvent) => {
      const dx = e.clientX - from.x
      const dy = e.clientY - from.y
      const w = Math.round(Math.min(window.innerWidth - 16, Math.max(SIZE_MIN.w, from.w + (east ? dx : -dx))))
      const h = Math.round(Math.min(window.innerHeight - 16, Math.max(SIZE_MIN.h, from.h + (south ? dy : -dy))))
      at = {
        w, h,
        x: east ? from.px + (w - from.w) : from.px,
        y: south ? from.py + (h - from.h) : from.py,
      }
      setSize({ w: at.w, h: at.h })
      setPos({ x: at.x, y: at.y })
    }
    const up = () => {
      grip.releasePointerCapture(down.pointerId)
      grip.removeEventListener('pointermove', move)
      grip.removeEventListener('pointerup', up)
      try {
        window.localStorage.setItem(SIZE_KEY, JSON.stringify({ w: at.w, h: at.h }))
        window.localStorage.setItem(POS_KEY, JSON.stringify({ x: at.x, y: at.y }))
      } catch {
        // Not remembering a size is not an error.
      }
    }
    grip.addEventListener('pointermove', move)
    grip.addEventListener('pointerup', up)
  }

  const startDrag = (down: React.PointerEvent) => {
    if (!popped) return
    // The dock button lives on this bar. Capturing the pointer here
    // redirects every later event to the bar, so the button never saw
    // its own click and the panel could be floated but never put back.
    if ((down.target as HTMLElement).closest('button, label, input')) return
    const handle = down.currentTarget as HTMLElement
    handle.setPointerCapture(down.pointerId)
    dragFrom.current = { x: down.clientX - pos.x, y: down.clientY - pos.y }
    const move = (e: PointerEvent) => {
      const from = dragFrom.current
      if (!from) return
      setPos({ x: e.clientX - from.x, y: e.clientY - from.y })
    }
    const up = () => {
      dragFrom.current = null
      handle.releasePointerCapture(down.pointerId)
      handle.removeEventListener('pointermove', move)
      handle.removeEventListener('pointerup', up)
      setPos((at) => {
        try {
          window.localStorage.setItem(POS_KEY, JSON.stringify(at))
        } catch {
          // Not remembering where a panel was put is not an error.
        }
        return at
      })
    }
    handle.addEventListener('pointermove', move)
    handle.addEventListener('pointerup', up)
  }

  // What the viewer chose to see on the picture. Remembered per camera
  // in this browser; the list offers only what this camera shows.
  const [layers, setLayers] = useState<OverlayLayer[]>([])
  const [hidden, setHidden] = useState<Set<string>>(() => new Set())
  useEffect(() => {
    if (cameraId == null) return
    try {
      const v = JSON.parse(window.localStorage.getItem(hiddenLayersKey(cameraId)) || '[]')
      setHidden(new Set(Array.isArray(v) ? v.filter((k) => typeof k === 'string') : []))
    } catch {
      setHidden(new Set())
    }
  }, [cameraId])
  const toggleLayer = (key: string) => setHidden((prev) => {
    const next = new Set(prev)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    if (cameraId != null) {
      try {
        window.localStorage.setItem(hiddenLayersKey(cameraId), JSON.stringify([...next]))
      } catch {
        // Not remembering a choice is not an error.
      }
    }
    return next
  })
  // Stable, so passing it down never restarts the player.
  const onLayers = useCallback((ls: OverlayLayer[]) => {
    setLayers([...ls].sort((a, b) => a.group.localeCompare(b.group) || a.name.localeCompare(b.name)))
  }, [])
  const [layersOpen, setLayersOpen] = useState(false)
  const layersRef = useRef<HTMLDivElement>(null)
  useClickOutside(layersRef, layersOpen, () => setLayersOpen(false))

  const [urls, setUrls] = useState<Urls | null>(null)
  const [tokenVersion, setTokenVersion] = useState(0)
  const { status, version: streamVersion } = useCameraStatus(cameraId)

  // Throttled: a burst of 401s must not become a burst of token mints.
  const lastRefreshRef = useRef(0)
  const handleAuthExpired = useCallback(() => {
    const now = Date.now()
    if (now - lastRefreshRef.current < 10_000) return
    lastRefreshRef.current = now
    setTokenVersion((v) => v + 1)
  }, [])

  useEffect(() => {
    if (cameraId == null) { setUrls(null); return }
    let alive = true
    ;(async () => {
      try {
        const { data } = await apiService.getStreamUrls(cameraId)
        if (!alive) return
        // Rebase onto the origin the UI is served from: the backend
        // pins these to the LAN IP, which breaks playback over
        // https://localhost (lib/streamUrl.ts).
        setUrls({
          whep: rebaseToCurrentOrigin((data as any).urls?.webrtc),
          hls: rebaseToCurrentOrigin((data as any).urls?.hls),
          token: (data as any).token,
        })
      } catch {
        // Say "no link" rather than keep a stale token that will 401.
        if (alive) setUrls(null)
      }
    })()
    return () => { alive = false }
  }, [cameraId, streamVersion, tokenVersion])

  const hasLink = !!urls?.whep || !!urls?.hls
  const offline = status === 'offline'

  return (
    // Popping out RESTYLES this element; it never moves in the tree.
    // Re-parenting a React subtree unmounts it, which would tear down
    // the WebRTC session and reconnect — the one thing a "pop out"
    // must not do to a live picture.
    <Card
      className={popped
        ? 'fixed z-40 flex flex-col shadow-2xl'
        : 'h-full'}
      style={popped
        ? { right: 24 - pos.x, bottom: 24 - pos.y, width: size.w, height: size.h }
        : undefined}
    >
      {popped && CORNERS.map(({ corner, place, cursor, path }) => (
        <div
          key={corner}
          role="separator"
          aria-label="Resize the floating camera"
          title="Drag to resize"
          onPointerDown={startResize(corner)}
          className={`absolute z-10 h-3 w-3 touch-none ${place} ${cursor}`}
        >
          {/* The usual two-stroke resize grip, drawn into its corner. */}
          <svg viewBox="0 0 16 16" className="h-3 w-3 text-[var(--text-dim)]" aria-hidden="true">
            <path d={path} stroke="currentColor" strokeWidth="1.5" fill="none" />
          </svg>
        </div>
      ))}
      <CardContent className="flex h-full flex-col gap-2 p-3">
        {/* No expand button of our own: the player carries one in its
            own controls — which its layout keeps at every size, down to
            the smallest tile — and a double-click on the picture does
            the same thing. A second control beside them would just be
            a third way to do one job. */}
        <div
          className={`flex shrink-0 items-center gap-2 pb-0.5 ${
            popped ? 'cursor-move select-none' : ''}`}
          onPointerDown={startDrag}
        >
          {cameras.length > 1 && onSelectCamera && cameraId != null ? (
            <select
              value={cameraId}
              onChange={(e) => onSelectCamera(Number(e.target.value))}
              aria-label="Camera to watch live"
              title="Switch between the cameras selected for this app"
              className="min-w-0 max-w-full truncate rounded border border-[var(--border)]
                         bg-[var(--bg-2)] px-1.5 py-0.5 text-xs font-semibold"
            >
              {cameras.map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
          ) : (
            <h3 className="min-w-0 truncate text-xs font-semibold" title={cameraName}>
              {cameraName || 'Live'}
            </h3>
          )}
          <div className="ml-auto flex items-center gap-0.5">
          {(layers.length > 0 || hidden.size > 0) && (
            <div ref={layersRef} className="relative">
              <button
                type="button"
                onClick={() => setLayersOpen((v) => !v)}
                aria-label="Choose what is drawn on the picture"
                aria-expanded={layersOpen}
                title="Choose what is drawn on the picture"
                className={`flex items-center gap-1 rounded px-1 py-0.5 text-[11px]
                            hover:bg-[var(--panel)] hover:text-[var(--text)] ${
                  hidden.size ? 'text-[var(--accent)]' : 'text-[var(--text-dim)]'}`}
              >
                <Layers size={13} />
                {hidden.size > 0 && <span>{hidden.size} off</span>}
              </button>
              {layersOpen && (
                <div className="absolute right-0 top-full z-50 mt-1 w-60 border border-[var(--border)]
                                bg-[var(--panel-2)] p-2 text-xs shadow-xl">
                  <div className="mb-1 flex items-center">
                    <span className="font-semibold">On the picture</span>
                    {hidden.size > 0 && (
                      <button type="button" className="ml-auto text-[var(--accent)] hover:underline"
                              onClick={() => {
                                setHidden(new Set())
                                if (cameraId != null) {
                                  try { window.localStorage.removeItem(hiddenLayersKey(cameraId)) } catch { /* fine */ }
                                }
                              }}>
                        Show all
                      </button>
                    )}
                  </div>
                  {layers.length === 0 ? (
                    <p className="text-[var(--text-dim)]">Nothing is being drawn right now.</p>
                  ) : (
                    [...new Set(layers.map((l) => l.group))].map((group) => (
                      <div key={group} className="mt-1">
                        <div className="text-[10px] uppercase tracking-wide text-[var(--text-dim)]">{group}</div>
                        {layers.filter((l) => l.group === group).map((l) => (
                          <label key={l.key} className="flex cursor-pointer items-center gap-2 py-0.5">
                            <input type="checkbox" checked={!hidden.has(l.key)}
                                   onChange={() => toggleLayer(l.key)} />
                            <span className="truncate" title={l.name}>{l.name}</span>
                          </label>
                        ))}
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          )}
          {onTogglePop && (
            <button
              type="button"
              onClick={onTogglePop}
              aria-label={popped ? 'Put the camera back in the page' : 'Float the camera'}
              title={popped
                ? 'Dock it back beside the list'
                : 'Float it — drag by this bar to put it where you want'}
              className="rounded p-1 text-[var(--text-dim)]
                         hover:bg-[var(--panel)] hover:text-[var(--text)]"
            >
              {/* Not an ✕. A cross on a floating panel reads as "close
                  this" — and closing a camera you only wanted back in
                  its place is not the same thing at all. */}
              {popped ? <Minimize2 size={13} /> : <PictureInPicture2 size={13} />}
            </button>
          )}
          </div>
        </div>

        <div className="min-h-[112px] flex-1 overflow-hidden rounded bg-black">
          {cameraId == null ? (
            <Empty>
              {emptyMessage ? <CameraOff size={20} className="mb-1" /> : null}
              {emptyMessage ?? 'Select a camera to watch it live.'}
              {onPickCameras && (
                <button
                  type="button"
                  onClick={onPickCameras}
                  className="mt-2 inline-flex items-center gap-1 rounded border border-[var(--accent)]
                             px-2 py-1 text-[11px] text-[var(--accent)] hover:bg-[var(--accent)]/10"
                >
                  <Settings2 size={12} /> Select cameras
                </button>
              )}
            </Empty>
          ) : offline ? (
            <Empty><VideoOff size={20} className="mb-1" />This camera is offline.</Empty>
          ) : hasLink ? (
            <VideoPlayer
              key={`${cameraId}-${streamVersion}-${tokenVersion}`}
              mode="live"
              whepUrl={urls?.whep}
              hlsUrl={urls?.hls}
              mediamtxToken={urls?.token}
              onAuthExpired={handleAuthExpired}
              cameraId={cameraId}
              showDetections
              overlayHidden={hidden}
              onOverlayLayers={onLayers}
              className="h-full w-full"
            />
          ) : (
            <Empty>No stream from this camera.</Empty>
          )}
        </div>

        {overlayEnabled === false && (
          <p className="text-[10px] leading-tight text-[var(--text-dim)]">
            Boxes are off: an administrator has not allowed this app to draw on live
            video, in the App Catalog.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center px-3
                    text-center text-[11px] text-[var(--text-dim)]">
      {children}
    </div>
  )
}
