/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// The entrance, right now, beside the figures about it.
//
// The rest of the page is history: what was screened, how it scored,
// who was flagged. This is the one panel that answers "what is
// happening at the door" — and with the app's own boxes drawn on it, it
// also shows the screening logic working, which is the difference
// between trusting a compliance figure and merely reading one.
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
import { PictureInPicture2, VideoOff, X } from 'lucide-react'
import { apiService } from '../../lib/apiService'
import { rebaseToCurrentOrigin } from '../../lib/streamUrl'
import { useCameraStatus } from '../../hooks/useCameraStatus'
import { VideoPlayer } from '../../components/VideoPlayer'
import { Card, CardContent } from '../../components/ui'

type Urls = { whep?: string; hls?: string; token?: string }

export function LiveCameraPanel({
  cameraId, cameraName, overlayEnabled, popped = false, onTogglePop,
}: {
  cameraId: number | null
  cameraName: string
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
  const [pos, setPos] = useState({ x: 0, y: 0 })
  const dragFrom = useRef<{ x: number; y: number } | null>(null)

  const startDrag = (down: React.PointerEvent) => {
    if (!popped) return
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
    }
    handle.addEventListener('pointermove', move)
    handle.addEventListener('pointerup', up)
  }

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
        ? 'fixed z-40 flex w-[420px] flex-col shadow-2xl'
        : 'h-full'}
      style={popped
        ? { right: 24 - pos.x, bottom: 24 - pos.y, height: 300 }
        : undefined}
    >
      <CardContent className="flex h-full flex-col gap-2 p-3">
        {/* No expand button of our own: the player carries one in its
            own controls — which its layout keeps at every size, down to
            the smallest tile — and a double-click on the picture does
            the same thing. A second control beside them would just be
            a third way to do one job. */}
        <div
          className={`flex items-center gap-2 ${popped ? 'cursor-move' : ''}`}
          onPointerDown={startDrag}
        >
          <h3 className="min-w-0 truncate text-xs font-semibold" title={cameraName}>
            {cameraName || 'Entrance'}
          </h3>
          {onTogglePop && (
            <button
              type="button"
              onClick={onTogglePop}
              aria-label={popped ? 'Put the camera back in the page' : 'Float the camera'}
              title={popped
                ? 'Dock it back beside the list'
                : 'Float it — drag by this bar to put it where you want'}
              className="ml-auto rounded p-1 text-[var(--text-dim)]
                         hover:bg-[var(--panel)] hover:text-[var(--text)]"
            >
              {popped ? <X size={13} /> : <PictureInPicture2 size={13} />}
            </button>
          )}
        </div>

        <div className="min-h-[112px] flex-1 overflow-hidden rounded bg-black">
          {cameraId == null ? (
            <Empty>Pick a camera to watch it live.</Empty>
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
