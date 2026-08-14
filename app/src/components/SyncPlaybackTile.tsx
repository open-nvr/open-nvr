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

import { useCallback, useEffect, useRef, useState } from 'react'
import type Hls from 'hls.js'
import { CameraOff, Loader2, Radio, AlertCircle, Volume2 } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { loadHls } from '../lib/loadHls'
import type { TimelineSegment } from './PlaybackTimeline'

interface SyncPlaybackTileProps {
  cameraId: number
  cameraName: string
  /** Footage blocks for the selected day (epoch ms, sorted). */
  segments: TimelineSegment[]
  /** Start of the still-recording file — footage there is not playable as VOD. */
  liveEdgeMs: number | null
  /** Shared wall-clock playhead (epoch ms). The tile follows it. */
  masterMs: number
  playing: boolean
  rate: number
  muted: boolean
  /** Highlighted tile (audio focus). */
  active: boolean
  onActivate: () => void
}

type TileStatus = 'idle' | 'loading' | 'ok' | 'gap' | 'live' | 'error'

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

/**
 * One camera in the synchronized playback grid. The tile is a pure follower
 * of the shared master clock: whenever the master instant leaves the loaded
 * media window it opens a per-clip byte-range HLS session anchored AT that
 * instant, and while inside the window it only drift-corrects. Clip
 * boundaries, gaps and the live edge all fall out of the same follow logic —
 * there is no per-tile transport state to get out of sync.
 */
export function SyncPlaybackTile({
  cameraId,
  cameraName,
  segments,
  liveEdgeMs,
  masterMs,
  playing,
  rate,
  muted,
  active,
  onActivate,
}: SyncPlaybackTileProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const hlsRef = useRef<Hls | null>(null)
  // The lazily-loaded hls.js module (see lib/loadHls); null until resolved.
  const hlsLibRef = useRef<typeof Hls | null>(null)
  const sessionIdRef = useRef<string | null>(null)
  // Only the latest load token wins; superseded async loads no-op.
  const loadTokenRef = useRef(0)
  // Wall-clock bounds of the media loaded into the <video>: startMs maps to
  // video t=0. endMs is refined once the real media duration is known — the
  // session may resolve a physical file shorter than the listed segment.
  const windowRef = useRef<{ startMs: number; endMs: number } | null>(null)
  // Target of the load currently in flight, so the follow loop doesn't spawn
  // a new session on every master tick while one is being set up.
  const inFlightRef = useRef<{ targetMs: number } | null>(null)
  // Damper: after a failed load, don't retry until the master moves on. Without
  // this a segment whose metadata overstates its media would cause an endless
  // create-session/404 loop.
  const failRef = useRef<{ targetMs: number } | null>(null)
  const masterRef = useRef(masterMs)
  const playingRef = useRef(playing)
  const rateRef = useRef(rate)
  const mutedRef = useRef(muted)

  const [status, setStatus] = useState<TileStatus>('idle')
  const [buffering, setBuffering] = useState(false)

  const teardownMedia = useCallback(() => {
    if (hlsRef.current) {
      hlsRef.current.destroy()
      hlsRef.current = null
    }
    const prev = sessionIdRef.current
    sessionIdRef.current = null
    if (prev) apiService.deleteHlsPlaybackSession(prev).catch(() => {})
  }, [])

  /** Seek the loaded media to the CURRENT master instant and apply transport. */
  const settleAtMaster = useCallback(() => {
    const el = videoRef.current
    const w = windowRef.current
    if (!el || !w) return
    const off = (masterRef.current - w.startMs) / 1000
    if (off > 0.05) {
      try {
        // Never land on the exact end — an at-end start fires 'ended' at once.
        el.currentTime = isFinite(el.duration) ? clamp(off, 0, Math.max(0, el.duration - 0.3)) : off
      } catch {
        /* seekable range not ready; starts at head */
      }
    }
    el.playbackRate = rateRef.current
    el.muted = mutedRef.current
    if (playingRef.current) el.play().catch(() => {})
  }, [])

  /**
   * Open a per-clip HLS session anchored at `targetMs` inside `seg`. Anchoring
   * at the target (not the clip start) means the media always begins at the
   * wanted instant and the backend resolves the physical file that actually
   * contains it — merged-file boundaries need no special casing.
   */
  const loadAt = useCallback(
    async (seg: TimelineSegment, targetMs: number) => {
      const el = videoRef.current
      if (!el) return
      const token = ++loadTokenRef.current
      const anchorMs = clamp(targetMs, seg.startMs, seg.endMs - 1500)
      inFlightRef.current = { targetMs: anchorMs }
      teardownMedia()
      windowRef.current = null
      setStatus('loading')

      let manifestUrl: string
      let browserMp4Url: string | null = null
      let fileOffsetSec = 0
      try {
        // Kick the hls.js chunk download in parallel with the session create;
        // both are needed before playback can start.
        const [res, HlsLib] = (await Promise.all([
          apiService.createHlsPlaybackSession({
            camera_id: cameraId,
            start: new Date(anchorMs).toISOString(),
            end: new Date(seg.endMs).toISOString(),
          }),
          loadHls().catch(() => null),
        ])) as [any, typeof Hls | null]
        hlsLibRef.current = HlsLib
        if (token !== loadTokenRef.current) return
        manifestUrl = res.data?.manifest_url
        sessionIdRef.current = res.data?.session_id || null
        if (res.data?.needs_remux && res.data?.browser_mp4_url) {
          browserMp4Url = res.data.browser_mp4_url
          fileOffsetSec = res.data.file_offset_seconds || 0
        }
        if (!manifestUrl && !browserMp4Url) throw new Error('No manifest returned')
      } catch {
        if (token === loadTokenRef.current) {
          inFlightRef.current = null
          failRef.current = { targetMs: anchorMs }
          setStatus('error')
        }
        return
      }

      const onReady = () => {
        if (token !== loadTokenRef.current) return
        inFlightRef.current = null
        failRef.current = null
        setStatus('ok')
        setBuffering(false)
        settleAtMaster()
      }

      if (browserMp4Url) {
        // H.265 path: server-remuxed hvc1 MP4 played natively. The remuxed
        // file is the whole physical on-disk file, beginning fileOffsetSec
        // before the session anchor — map the window to the file so every
        // in-file seek is a native HTTP-Range seek.
        const fileStartMs = anchorMs - fileOffsetSec * 1000
        el.src = browserMp4Url
        el.addEventListener(
          'loadedmetadata',
          () => {
            if (token !== loadTokenRef.current) return
            windowRef.current = {
              startMs: fileStartMs,
              endMs: fileStartMs + (isFinite(el.duration) ? el.duration * 1000 : seg.endMs - fileStartMs),
            }
            onReady()
          },
          { once: true }
        )
        el.addEventListener(
          'error',
          () => {
            if (token === loadTokenRef.current) {
              inFlightRef.current = null
              failRef.current = { targetMs: anchorMs }
              setBuffering(false)
              setStatus('error')
            }
          },
          { once: true }
        )
      } else if (hlsLibRef.current?.isSupported()) {
        const HlsLib = hlsLibRef.current
        const hls = new HlsLib({
          enableWorker: true,
          lowLatencyMode: false,
          backBufferLength: 10,
          maxBufferLength: 30,
          maxMaxBufferLength: 60,
        })
        hlsRef.current = hls
        hls.loadSource(manifestUrl)
        hls.attachMedia(el)
        hls.on(HlsLib.Events.MANIFEST_PARSED, () => {
          if (token !== loadTokenRef.current) return
          windowRef.current = { startMs: anchorMs, endMs: seg.endMs }
          onReady()
        })
        // Refine the window end with the real media duration: the session only
        // covers the physical file containing its anchor, which can be shorter
        // than the listed (merged) segment. Crossing that boundary then simply
        // triggers the next anchored session.
        hls.on(HlsLib.Events.LEVEL_LOADED, (_evt, data) => {
          if (token !== loadTokenRef.current) return
          const dur = data?.details?.totalduration
          if (dur && windowRef.current) {
            windowRef.current = { startMs: anchorMs, endMs: anchorMs + dur * 1000 }
          }
        })
        hls.on(HlsLib.Events.ERROR, (_evt, data) => {
          if (!data.fatal) return
          if (data.type === HlsLib.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError()
          else if (data.type === HlsLib.ErrorTypes.NETWORK_ERROR) hls.startLoad()
        })
      } else if (el.canPlayType('application/vnd.apple.mpegurl')) {
        el.src = manifestUrl
        el.addEventListener(
          'loadedmetadata',
          () => {
            if (token !== loadTokenRef.current) return
            windowRef.current = {
              startMs: anchorMs,
              endMs: anchorMs + (isFinite(el.duration) ? el.duration * 1000 : seg.endMs - anchorMs),
            }
            onReady()
          },
          { once: true }
        )
      } else {
        inFlightRef.current = null
        setStatus('error')
      }
    },
    [cameraId, teardownMedia, settleAtMaster]
  )

  // ---- The follow loop: reconcile the tile against the master clock --------
  useEffect(() => {
    masterRef.current = masterMs
    playingRef.current = playing
    rateRef.current = rate
    mutedRef.current = muted

    const el = videoRef.current
    if (!el) return
    const m = masterMs

    // Live zone: footage here is still being written — not playable as VOD.
    if (liveEdgeMs != null && m >= liveEdgeMs) {
      if (!el.paused) el.pause()
      setStatus('live')
      return
    }

    const seg = segments.find((s) => m >= s.startMs - 200 && m < s.endMs)
    if (!seg) {
      if (!el.paused) el.pause()
      setStatus(segments.length ? 'gap' : 'idle')
      return
    }

    const w = windowRef.current
    const inWindow = w && m >= w.startMs - 300 && m < w.endMs - 300
    if (!inWindow) {
      // A load for (roughly) this instant is already in flight — let it land;
      // it settles at the then-current master. A far-away target supersedes it.
      if (inFlightRef.current && Math.abs(inFlightRef.current.targetMs - m) < 10_000) return
      // Recent failure at this spot — wait for the master to move on.
      if (failRef.current && Math.abs(failRef.current.targetMs - m) < 5_000) return
      loadAt(seg, m)
      return
    }

    // Inside the loaded media: drift-correct and apply transport state.
    if (el.readyState >= 1) {
      const wallMs = w.startMs + el.currentTime * 1000
      const tolMs = Math.max(1500, rate * 500)
      if (Math.abs(wallMs - m) > tolMs) {
        try {
          el.currentTime = clamp(
            (m - w.startMs) / 1000,
            0,
            isFinite(el.duration) ? Math.max(0, el.duration - 0.3) : Number.MAX_SAFE_INTEGER
          )
        } catch {
          /* not seekable yet */
        }
      }
    }
    if (el.playbackRate !== rate) el.playbackRate = rate
    if (el.muted !== muted) el.muted = muted
    if (playing && el.paused && !el.ended) el.play().catch(() => {})
    else if (!playing && !el.paused) el.pause()
    setStatus('ok')
  }, [masterMs, playing, rate, muted, segments, liveEdgeMs, loadAt])

  // ---- Video element events ------------------------------------------------
  useEffect(() => {
    const el = videoRef.current
    if (!el) return
    const onWaiting = () => setBuffering(true)
    const onPlaying = () => setBuffering(false)
    const onCanPlay = () => setBuffering(false)
    const onEnded = () => {
      // Media ran out before the listed segment end (durations can overstate,
      // or the segment spans multiple physical files): shrink the window to
      // what actually played so the follow loop opens the next session.
      const w = windowRef.current
      if (w) windowRef.current = { startMs: w.startMs, endMs: w.startMs + el.currentTime * 1000 }
    }
    el.addEventListener('waiting', onWaiting)
    el.addEventListener('playing', onPlaying)
    el.addEventListener('canplay', onCanPlay)
    el.addEventListener('ended', onEnded)
    return () => {
      el.removeEventListener('waiting', onWaiting)
      el.removeEventListener('playing', onPlaying)
      el.removeEventListener('canplay', onCanPlay)
      el.removeEventListener('ended', onEnded)
    }
  }, [])

  // Cleanup on unmount (also covers deselecting the camera).
  useEffect(() => {
    return () => {
      teardownMedia()
      const el = videoRef.current
      if (el) {
        el.pause()
        el.removeAttribute('src')
        el.load()
      }
    }
  }, [teardownMedia])

  const showSpinner = status === 'loading' || (status === 'ok' && buffering)

  return (
    <div
      onClick={onActivate}
      className={`relative bg-black overflow-hidden cursor-pointer aspect-video lg:aspect-auto border ${
        active ? 'border-[var(--accent)]' : 'border-[var(--border)]'
      }`}
    >
      <video ref={videoRef} className="w-full h-full object-contain" playsInline crossOrigin="anonymous" />

      {/* Name chip */}
      <div className="absolute top-1 left-1 flex items-center gap-1 max-w-[85%] px-1.5 py-0.5 bg-black/60 text-white text-[10px] leading-tight">
        <span className="truncate">{cameraName}</span>
        {active && !muted && <Volume2 size={10} className="shrink-0 text-[var(--accent)]" />}
      </div>

      {showSpinner && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/30 pointer-events-none">
          <Loader2 size={26} className="animate-spin text-[var(--accent)]" />
        </div>
      )}

      {(status === 'gap' || status === 'idle') && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-black/70 pointer-events-none">
          <CameraOff size={22} className="text-neutral-500" />
          <span className="text-[11px] text-neutral-400">No recording at this time</span>
        </div>
      )}

      {status === 'live' && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-black/70 pointer-events-none">
          <Radio size={22} className="text-green-500 animate-pulse" />
          <span className="text-[11px] text-neutral-300">Still recording — watch in Live View</span>
        </div>
      )}

      {status === 'error' && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-black/70 pointer-events-none">
          <AlertCircle size={22} className="text-amber-400" />
          <span className="text-[11px] text-neutral-300">Playback failed here</span>
        </div>
      )}
    </div>
  )
}
