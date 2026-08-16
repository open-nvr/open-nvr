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
import { useNavigate } from 'react-router-dom'
import type Hls from 'hls.js'
import {
  Play,
  Pause,
  SkipBack,
  SkipForward,
  Rewind,
  FastForward,
  Maximize2,
  Minimize2,
  Volume2,
  VolumeX,
  Loader2,
  X,
  AlertCircle,
  Scissors,
  Download,
  Radio,
} from 'lucide-react'
import { apiService } from '../lib/apiService'
import { loadHls, warmHls } from '../lib/loadHls'
import { useCameraSegments } from '../lib/queries'
import { localDayEnd, localDayStart } from '../lib/time'
import { PlaybackTimeline, type TimelineSegment } from './PlaybackTimeline'

interface RawSegment {
  start: string
  duration: number
  playback_url: string
}

interface Seg {
  startMs: number
  endMs: number
  startIso: string
  duration: number
}

interface PlaybackConsoleProps {
  cameraId: number
  cameraName: string
  /** YYYY-MM-DD */
  date: string
  onClose: () => void
}

// Zoom presets (visible span in ms) — mirrors the "1 Day / …" control.
const ZOOMS: { label: string; span: number }[] = [
  { label: '24h', span: 24 * 3600_000 },
  { label: '6h', span: 6 * 3600_000 },
  { label: '1h', span: 3600_000 },
  { label: '10m', span: 600_000 },
  { label: '2m', span: 120_000 },
]

const SPEEDS = [0.25, 0.5, 1, 2, 4, 8, 16]
const RATE_1X = 2 // index of 1x in SPEEDS
const FRAME_STEP = 1 / 25 // ~1 frame at 25fps

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

function parseSegments(raw: RawSegment[]): Seg[] {
  return raw
    .map((r) => {
      const startMs = Date.parse(r.start)
      return {
        startMs,
        endMs: startMs + (r.duration || 0) * 1000,
        startIso: r.start,
        duration: r.duration || 0,
      }
    })
    .filter((s) => Number.isFinite(s.startMs))
    .sort((a, b) => a.startMs - b.startMs)
}

const sameSegs = (a: Seg[], b: Seg[]) =>
  a.length === b.length &&
  a.every((s, i) => s.startMs === b[i].startMs && s.endMs === b[i].endMs)

export function PlaybackConsole({ cameraId, cameraName, date, onClose }: PlaybackConsoleProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const hlsRef = useRef<Hls | null>(null)
  // The lazily-loaded hls.js module (see lib/loadHls); null until resolved.
  const hlsLibRef = useRef<typeof Hls | null>(null)

  // Start the hls.js chunk download while segments are being fetched.
  useEffect(() => {
    warmHls()
  }, [])
  const sessionIdRef = useRef<string | null>(null)
  // Guards against out-of-order async loads: only the latest load token wins.
  const loadTokenRef = useRef(0)
  // Wall-clock instant (epoch ms) the loaded clip begins at, for time mapping.
  const windowStartRef = useRef<number>(0)
  // Bounds of the clip currently loaded into the <video>. Seeks inside it are
  // native (hls.js fetches the right byte-range fragment → instant); only
  // crossing into another clip spins up a new session.
  const loadedClipRef = useRef<{ startMs: number; endMs: number } | null>(null)
  // Latest segments, mirrored to a ref so the poll loop reads them without
  // re-subscribing every update.
  const segsRef = useRef<Seg[]>([])
  // Detects 'ended' firing repeatedly at the same instant (metadata overstates
  // the media) so onEnded can advance instead of re-loading the same clip.
  const endedGuardRef = useRef<{ afterMs: number; count: number } | null>(null)
  // Start of the STILL-RECORDING file (epoch ms). Footage from here on is not
  // reliably playable as VOD (the file's bytes shift while it grows), so the
  // console refuses to open a session there and offers Live View instead.
  const liveEdgeRef = useRef<number | null>(null)

  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [segs, setSegs] = useState<Seg[]>([])
  const [liveEdgeMs, setLiveEdgeMs] = useState<number | null>(null)
  // Shown when the user (or auto-advance) reaches the live zone.
  const [livePrompt, setLivePrompt] = useState(false)
  // MediaMTX browser-reachable playback base + path, for clip export.
  const [base, setBase] = useState('')
  const [path, setPath] = useState('')

  const [dayStart, setDayStart] = useState(0)
  const [dayEnd, setDayEnd] = useState(0)
  const [view, setView] = useState<{ start: number; end: number }>({ start: 0, end: 0 })
  const [zoomIdx, setZoomIdx] = useState(0)

  const [currentMs, setCurrentMs] = useState(0)
  const [previewMs, setPreviewMs] = useState<number | null>(null)
  const [isPlaying, setIsPlaying] = useState(false)
  const [rateIdx, setRateIdx] = useState(RATE_1X)
  const [muted, setMuted] = useState(false)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [buffering, setBuffering] = useState(false)

  // Clip-export state.
  const [clipMode, setClipMode] = useState(false)
  const [selection, setSelection] = useState<{ inMs: number; outMs: number } | null>(null)
  const [exporting, setExporting] = useState(false)

  const timelineSegs: TimelineSegment[] = useMemo(
    () => segs.map((s) => ({ startMs: s.startMs, endMs: s.endMs })),
    [segs]
  )

  // ---- Load the day's segments (react-query; polls today at 10s) -----------
  // Polling only runs while `date` is the local today (footage still being
  // written); historical days are cached and never refetched on an interval.
  const segQuery = useCameraSegments(cameraId, date, { poll: true, pollMs: 10_000 })

  // Init-once guard per camera+day: the day window/playhead initialize on the
  // first data arrival, later poll updates only grow the timeline.
  const initKeyRef = useRef<string | null>(null)

  useEffect(() => {
    if (segQuery.isPending) {
      setLoading(true)
      setError(null)
      return
    }
    if (segQuery.error) {
      setError((segQuery.error as any)?.message || 'Failed to load recordings')
      setLoading(false)
      return
    }
    const data: any = segQuery.data
    const parsed = parseSegments(data?.segments || [])

    setBase((data?.playback_base_url || '').replace(/\/$/, ''))
    setPath(data?.path || '')
    // sameSegs guard: unchanged poll responses must not replace state (and
    // cascade re-renders/effects downstream).
    if (!sameSegs(segsRef.current, parsed)) setSegs(parsed)

    const edge = data?.live_edge_start ? Date.parse(data.live_edge_start) : NaN
    const edgeMs = Number.isFinite(edge) ? edge : null
    if (edgeMs !== liveEdgeRef.current) {
      liveEdgeRef.current = edgeMs
      setLiveEdgeMs(edgeMs)
    }

    const initKey = `${cameraId}:${date}`
    if (initKeyRef.current !== initKey) {
      if (parsed.length === 0) {
        setError('No recordings found for this day.')
        setLoading(false)
        return
      }
      initKeyRef.current = initKey
      // Day window = the selected LOCAL day (midnight -> midnight), matching
      // the API's local-day segment range.
      const ds = localDayStart(date)
      const de = localDayEnd(date)
      setDayStart(ds)
      setDayEnd(de)
      setView({ start: ds, end: de })
      setZoomIdx(0)
      setCurrentMs(parsed[0].startMs)
      windowStartRef.current = parsed[0].startMs
    }
    setLoading(false)
  }, [segQuery.data, segQuery.isPending, segQuery.error, cameraId, date])

  // Mirror segments to a ref so async callbacks read the latest list.
  useEffect(() => {
    segsRef.current = segs
  }, [segs])

  // ---- Clip loading via per-clip byte-range HLS session ---------------------
  const teardownHls = useCallback(() => {
    if (hlsRef.current) {
      hlsRef.current.destroy()
      hlsRef.current = null
    }
    const prev = sessionIdRef.current
    sessionIdRef.current = null
    if (prev) apiService.deleteHlsPlaybackSession(prev).catch(() => {})
  }, [])

  /**
   * Load `clip` and seek to `offsetSec` within it. Each clip gets its own HLS
   * session whose manifest is a #EXT-X-BYTERANGE playlist over the single
   * on-disk file — so hls.js seeks with ranged reads (instant), and gaps
   * between clips are never crossed in one stream.
   */
  const loadClip = useCallback(
    async (clip: Seg, offsetSec: number, play: boolean, atTarget = false) => {
      const el = videoRef.current
      if (!el) return
      // LIVE-zone guard: the target instant falls inside the still-recording
      // file. A VOD session there downloads endlessly and never plays (the
      // file's bytes shift as it grows) — offer Live View instead.
      const edge = liveEdgeRef.current
      if (edge != null && clip.startMs + offsetSec * 1000 >= edge) {
        el.pause()
        setBuffering(false)
        setIsPlaying(false)
        setLivePrompt(true)
        return
      }
      const token = ++loadTokenRef.current

      teardownHls()

      windowStartRef.current = clip.startMs
      loadedClipRef.current = { startMs: clip.startMs, endMs: clip.endMs }
      setCurrentMs(clip.startMs + offsetSec * 1000)
      setBuffering(true)

      // MediaMTX merges contiguous recording files into ONE listed segment, but
      // a playback session only ever resolves the single on-disk file that
      // contains its START time. When the seek target turns out to lie beyond
      // that first file (detected below after metadata loads), we retry ONCE
      // with the session anchored at the absolute target instant so the backend
      // resolves the physical file that actually contains it.
      const targetMs = clamp(clip.startMs + offsetSec * 1000, clip.startMs, clip.endMs - 1000)
      const sessionStartIso = atTarget ? new Date(targetMs).toISOString() : clip.startIso
      const sessionStartMs = atTarget ? targetMs : clip.startMs

      let manifestUrl: string
      // H.265 (hev1) recordings can't play through hls.js/MSE as recorded (the
      // browser rejects the hev1 tag + PCM audio). The backend flags those and
      // exposes a server-remuxed, browser-playable hvc1 MP4 we play natively.
      let browserMp4Url: string | null = null
      // Seconds from the remuxed file's start to this clip's start — lets the
      // native player map the whole physical file's timeline (so seeks across it
      // are native, no reload).
      let fileOffsetSec = 0
      try {
        // hls.js is lazy-loaded; fetch it in parallel with the session create.
        const [res, HlsLib] = (await Promise.all([
          apiService.createHlsPlaybackSession({
            camera_id: cameraId,
            start: sessionStartIso,
            end: new Date(clip.endMs).toISOString(),
          }),
          loadHls().catch(() => null),
        ])) as [any, typeof Hls | null]
        hlsLibRef.current = HlsLib
        if (token !== loadTokenRef.current) return // superseded by a newer load
        manifestUrl = res.data?.manifest_url
        sessionIdRef.current = res.data?.session_id || null
        if (res.data?.needs_remux && res.data?.browser_mp4_url) {
          browserMp4Url = res.data.browser_mp4_url
          fileOffsetSec = res.data.file_offset_seconds || 0
        }
        if (!manifestUrl && !browserMp4Url) throw new Error('No manifest returned')
      } catch {
        if (token === loadTokenRef.current) {
          setBuffering(false)
          setError('Failed to start playback for this clip.')
        }
        return
      }

      // When the session is anchored at the target instant, the media begins AT
      // the target — no client-side seek needed, and the window starts there.
      if (atTarget) {
        windowStartRef.current = sessionStartMs
        loadedClipRef.current = { startMs: sessionStartMs, endMs: clip.endMs }
      }
      const startOffsetSec = atTarget ? 0 : offsetSec

      const startAtOffset = () => {
        if (token !== loadTokenRef.current) return
        el.playbackRate = SPEEDS[rateIdx]
        el.muted = muted
        if (startOffsetSec > 0.05) {
          try {
            el.currentTime = startOffsetSec
          } catch {
            /* seekable range not ready; starts at head */
          }
        }
        if (play) el.play().catch(() => {})
        setBuffering(false)
      }

      if (browserMp4Url) {
        // Native <video> playback of the server-remuxed hvc1 MP4 (H.265 path).
        // The remuxed file is the WHOLE physical on-disk file resolved from the
        // session start; it begins `fileOffsetSec` before that start. Anchor the
        // timeline at the file's start (video t=0) so wall-clock <-> video time
        // maps correctly, and set the loaded window to the whole file so EVERY
        // seek within it is a native HTTP-Range seek — not a session re-create
        // + full re-download (which is what made scrubbing re-fetch the segment
        // each time).
        const fileStartMs = sessionStartMs - fileOffsetSec * 1000
        windowStartRef.current = fileStartMs
        const initialTimeSec = Math.max(0, (targetMs - fileStartMs) / 1000)
        el.src = browserMp4Url
        el.addEventListener(
          'loadedmetadata',
          () => {
            if (token !== loadTokenRef.current) return
            // Target beyond this file's actual media → it lives in a LATER file
            // of the same merged segment. Retry once, anchored at the target.
            if (!atTarget && isFinite(el.duration) && initialTimeSec > el.duration - 0.25) {
              loadClip(clip, offsetSec, play, true)
              return
            }
            loadedClipRef.current = {
              startMs: fileStartMs,
              endMs: fileStartMs + (el.duration || 0) * 1000,
            }
            el.playbackRate = SPEEDS[rateIdx]
            el.muted = muted
            if (initialTimeSec > 0.05) {
              try {
                // Never seek to the exact end: an at-end start would fire
                // 'ended' immediately and bounce.
                el.currentTime = isFinite(el.duration)
                  ? Math.min(initialTimeSec, Math.max(0, el.duration - 0.5))
                  : initialTimeSec
              } catch {
                /* seekable range not ready; starts at head */
              }
            }
            if (play) el.play().catch(() => {})
            setBuffering(false)
          },
          { once: true }
        )
        el.addEventListener(
          'error',
          () => {
            if (token === loadTokenRef.current) {
              setBuffering(false)
              setError('Failed to play this recording.')
            }
          },
          { once: true }
        )
      } else if (hlsLibRef.current?.isSupported()) {
        const HlsLib = hlsLibRef.current
        const hls = new HlsLib({
          enableWorker: true,
          lowLatencyMode: false,
          backBufferLength: 30,
          maxBufferLength: 60,
          maxMaxBufferLength: 120,
        })
        hlsRef.current = hls
        hls.loadSource(manifestUrl)
        hls.attachMedia(el)
        hls.on(HlsLib.Events.MANIFEST_PARSED, startAtOffset)
        hls.on(HlsLib.Events.ERROR, (_evt, data) => {
          if (!data.fatal) return
          if (data.type === HlsLib.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError()
          else if (data.type === HlsLib.ErrorTypes.NETWORK_ERROR) hls.startLoad()
        })
      } else if (el.canPlayType('application/vnd.apple.mpegurl')) {
        el.src = manifestUrl
        el.addEventListener('loadedmetadata', startAtOffset, { once: true })
      } else {
        setBuffering(false)
        setError('HLS is not supported in this browser.')
      }
    },
    [cameraId, rateIdx, muted, teardownHls]
  )

  // Start playback at the first clip once segments are ready.
  useEffect(() => {
    if (!loading && segs.length > 0 && !sessionIdRef.current && !loadedClipRef.current) {
      loadClip(segs[0], 0, true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, segs])

  // ---- Video element events -------------------------------------------------
  useEffect(() => {
    const el = videoRef.current
    if (!el) return
    const onPlay = () => setIsPlaying(true)
    const onPause = () => setIsPlaying(false)
    const onTime = () => setCurrentMs(windowStartRef.current + el.currentTime * 1000)
    const onWaiting = () => setBuffering(true)
    const onPlaying = () => setBuffering(false)
    const onCanPlay = () => setBuffering(false)
    const onEnded = () => {
      const after = loadedClipRef.current?.endMs ?? windowStartRef.current + (el.duration || 0) * 1000
      // Loop guard: if 'ended' keeps firing at the same instant, the segment
      // metadata claims more footage than the media actually holds (MediaMTX
      // durations can overstate by a few seconds). Without this, the "grown"
      // reload below re-loads the same clip, seeks to its end, ends again —
      // an infinite session-create/fetch loop that shows up as flickering.
      const g = endedGuardRef.current
      const stuck = g !== null && Math.abs(after - g.afterMs) < 300 && g.count >= 1
      endedGuardRef.current =
        g !== null && Math.abs(after - g.afterMs) < 300
          ? { afterMs: g.afterMs, count: g.count + 1 }
          : { afterMs: after, count: 0 }
      // The current clip may have grown since we loaded it (live recording) —
      // reload it from where we stopped to play into the newly-written tail.
      const grown = stuck ? undefined : segs.find((s) => s.startMs <= after && after < s.endMs - 500)
      if (grown) {
        loadClip(grown, Math.max(0, (after - grown.startMs) / 1000), true)
        return
      }
      // Otherwise advance to the next clip (skips the grey gap).
      const next = segs.find((s) => s.startMs >= after + (stuck ? 300 : -500))
      if (next) loadClip(next, 0, true)
      else setIsPlaying(false)
    }
    el.addEventListener('play', onPlay)
    el.addEventListener('pause', onPause)
    el.addEventListener('timeupdate', onTime)
    el.addEventListener('waiting', onWaiting)
    el.addEventListener('playing', onPlaying)
    el.addEventListener('canplay', onCanPlay)
    el.addEventListener('ended', onEnded)
    return () => {
      el.removeEventListener('play', onPlay)
      el.removeEventListener('pause', onPause)
      el.removeEventListener('timeupdate', onTime)
      el.removeEventListener('waiting', onWaiting)
      el.removeEventListener('playing', onPlaying)
      el.removeEventListener('canplay', onCanPlay)
      el.removeEventListener('ended', onEnded)
    }
  }, [segs, loadClip])

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      teardownHls()
      const el = videoRef.current
      if (el) {
        el.pause()
        el.removeAttribute('src')
        el.load()
      }
    }
  }, [teardownHls])

  // Fullscreen tracking.
  useEffect(() => {
    const onFs = () => setIsFullscreen(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', onFs)
    return () => document.removeEventListener('fullscreenchange', onFs)
  }, [])

  // Keep the playhead within the visible window while playing (auto-follow).
  useEffect(() => {
    if (previewMs != null) return
    if (currentMs < view.start || currentMs > view.end) {
      const span = view.end - view.start
      if (span <= 0) return
      let start = clamp(currentMs - span / 2, dayStart, dayEnd - span)
      if (span >= dayEnd - dayStart) start = dayStart
      setView({ start, end: start + span })
    }
  }, [currentMs, previewMs, view.start, view.end, dayStart, dayEnd])

  // ---- Controls -------------------------------------------------------------
  const togglePlay = () => {
    const el = videoRef.current
    if (!el) return
    if (el.paused) el.play().catch(() => {})
    else el.pause()
  }

  const seekTo = (ms: number) => {
    const el = videoRef.current
    const c = loadedClipRef.current
    // LIVE zone → no VOD session; offer Live View.
    const edge = liveEdgeRef.current
    if (edge != null && ms >= edge) {
      el?.pause()
      setIsPlaying(false)
      setLivePrompt(true)
      return
    }
    setLivePrompt(false)
    // Inside the loaded clip → native seek. hls.js turns this into a ranged
    // fragment fetch, so it's instant both directions.
    if (el && c && ms >= c.startMs && ms < c.endMs) {
      el.currentTime = clamp((ms - c.startMs) / 1000, 0, el.duration || Number.MAX_SAFE_INTEGER)
      setCurrentMs(ms)
      if (isPlaying) el.play().catch(() => {})
      return
    }
    // Otherwise resolve the target clip (or the next one across a gap) and load.
    const clip = segs.find((s) => ms >= s.startMs && ms < s.endMs) || segs.find((s) => s.startMs >= ms)
    if (clip) loadClip(clip, Math.max(0, (ms - clip.startMs) / 1000), isPlaying)
  }

  const stepFrame = (dir: 1 | -1) => {
    const el = videoRef.current
    if (!el) return
    el.pause()
    el.currentTime = clamp(el.currentTime + dir * FRAME_STEP, 0, el.duration || 0)
  }

  const changeSpeed = (dir: 1 | -1) => {
    const next = clamp(rateIdx + dir, 0, SPEEDS.length - 1)
    setRateIdx(next)
    if (videoRef.current) videoRef.current.playbackRate = SPEEDS[next]
  }

  const toggleMute = () => {
    const el = videoRef.current
    if (!el) return
    el.muted = !el.muted
    setMuted(el.muted)
  }

  const toggleFullscreen = () => {
    if (document.fullscreenElement) document.exitFullscreen?.()
    else containerRef.current?.requestFullscreen?.()
  }

  // ---- Clip export ----------------------------------------------------------
  const toggleClipMode = () => {
    setClipMode((on) => {
      if (!on) videoRef.current?.pause() // entering clip mode: pause for precision
      setSelection(null)
      return !on
    })
  }

  const stamp = (ms: number) =>
    new Date(ms).toISOString().replace('T', '_').replace(/[:.]/g, '-').slice(0, 19)

  const exportClip = async () => {
    if (!selection || selection.outMs <= selection.inMs || !base || !path) return
    setExporting(true)
    try {
      const startIso = new Date(selection.inMs).toISOString()
      const durationSec = Math.max(1, (selection.outMs - selection.inMs) / 1000)
      // Authenticated backend export proxy: streams the clip to disk (the
      // old direct-MediaMTX fetch buffered the WHOLE clip in browser memory —
      // multi-GB for long selections — and carried no credential).
      const res: any = await apiService.createClipExportTicket({
        camera_id: cameraId,
        start: startIso,
        duration: durationSec,
        filename: `${cameraName.replace(/\s+/g, '-')}_${stamp(selection.inMs)}.mp4`,
      })
      const url = res.data?.download_url
      if (!url) throw new Error('No download URL returned')
      const a = document.createElement('a')
      a.href = url
      document.body.appendChild(a)
      a.click()
      a.remove()
      setClipMode(false)
      setSelection(null)
    } catch (e) {
      console.error('[Playback] Clip export failed:', e)
    } finally {
      setExporting(false)
    }
  }

  const selectionSeconds =
    selection && selection.outMs > selection.inMs
      ? Math.round((selection.outMs - selection.inMs) / 1000)
      : 0

  const applyZoom = (idx: number) => {
    const span = ZOOMS[idx].span
    setZoomIdx(idx)
    const center = previewMs ?? currentMs
    const total = dayEnd - dayStart
    if (span >= total) {
      setView({ start: dayStart, end: dayEnd })
      return
    }
    const start = clamp(center - span / 2, dayStart, dayEnd - span)
    setView({ start, end: start + span })
  }

  // Edge auto-scroll while scrubbing: shift the window, keep the span (and
  // the active zoom preset). Returning the same object when clamped-unchanged
  // avoids re-render churn at the day boundaries.
  const panBy = useCallback(
    (deltaMs: number) => {
      setView((v) => {
        const span = v.end - v.start
        if (span <= 0) return v
        const start = clamp(v.start + deltaMs, dayStart, Math.max(dayStart, dayEnd - span))
        return start === v.start ? v : { start, end: start + span }
      })
    },
    [dayStart, dayEnd]
  )

  // Continuous wheel zoom, anchored so the time under the cursor stays fixed.
  const zoomAt = useCallback(
    (anchorMs: number, factor: number) => {
      setZoomIdx(-1) // no preset exactly matches a free-form zoom
      setView((v) => {
        const span = v.end - v.start
        const total = dayEnd - dayStart
        const MIN_SPAN = 10_000 // don't zoom tighter than 10s
        const newSpan = clamp(span * factor, MIN_SPAN, total)
        if (newSpan >= total) return { start: dayStart, end: dayEnd }
        const rel = span > 0 ? (anchorMs - v.start) / span : 0.5
        const start = clamp(anchorMs - rel * newSpan, dayStart, dayEnd - newSpan)
        return { start, end: start + newSpan }
      })
    },
    [dayStart, dayEnd]
  )

  const effectiveCurrent = previewMs ?? currentMs

  return (
    <div
      className={`fixed inset-0 bg-black/85 flex items-center justify-center z-50 ${
        isFullscreen ? '' : 'p-4'
      }`}
    >
      <div
        ref={containerRef}
        className={`flex flex-col ${
          isFullscreen
            ? 'bg-black w-screen h-screen'
            : 'bg-[var(--panel)] border border-neutral-700 w-full max-w-5xl'
        }`}
      >
        {/* Header */}
        <div className="shrink-0 flex items-center justify-between px-4 py-2.5 border-b border-neutral-700 bg-[var(--panel-2)]">
          <div className="flex items-center gap-2 min-w-0">
            <Play size={16} className="text-[var(--accent)] shrink-0" />
            <span className="font-medium truncate">{cameraName}</span>
            <span className="text-xs text-[var(--text-dim)] shrink-0">· {date}</span>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 hover:bg-[var(--panel)] rounded transition-colors"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {/* Video */}
        <div
          className={`relative bg-black flex items-center justify-center ${
            isFullscreen ? 'flex-1 min-h-0' : 'aspect-video'
          }`}
        >
          {error ? (
            <div className="text-center p-8">
              <AlertCircle size={48} className="mx-auto mb-3 text-amber-400 opacity-70" />
              <p className="text-neutral-300 text-sm">{error}</p>
            </div>
          ) : (
            <>
              <video
                ref={videoRef}
                className="w-full h-full object-contain"
                playsInline
                crossOrigin="anonymous"
                onClick={togglePlay}
              />
              {(loading || buffering) && !livePrompt && (
                <div className="absolute inset-0 flex items-center justify-center bg-black/30 pointer-events-none">
                  <Loader2 size={40} className="animate-spin text-[var(--accent)]" />
                </div>
              )}
              {livePrompt && (
                <div className="absolute inset-0 flex items-center justify-center bg-black/70">
                  <div className="text-center p-6 max-w-sm">
                    <Radio size={36} className="mx-auto mb-3 text-green-500 animate-pulse" />
                    <p className="text-sm text-neutral-200 mb-1 font-medium">
                      You've reached the live edge
                    </p>
                    <p className="text-xs text-neutral-400 mb-4">
                      This part is still being recorded and will appear here once
                      it's finished. Watch what's happening now in Live View.
                    </p>
                    <div className="flex items-center justify-center gap-2">
                      <button
                        onClick={() => {
                          navigate('/live')
                          onClose()
                        }}
                        className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-green-600 text-white text-xs font-medium hover:opacity-90 transition-opacity"
                      >
                        <Radio size={13} /> Open Live View
                      </button>
                      <button
                        onClick={() => setLivePrompt(false)}
                        className="px-3 py-1.5 rounded border border-neutral-600 text-xs text-neutral-300 hover:bg-white/5 transition-colors"
                      >
                        Stay in playback
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        {/* Toolbar */}
        {!error && (
          <div className="shrink-0 flex items-center gap-1 px-3 py-2 bg-[var(--panel-2)] border-t border-neutral-700">
            <IconBtn title="Previous frame" onClick={() => stepFrame(-1)}>
              <SkipBack size={16} />
            </IconBtn>
            <IconBtn title={isPlaying ? 'Pause' : 'Play'} onClick={togglePlay}>
              {isPlaying ? <Pause size={18} /> : <Play size={18} />}
            </IconBtn>
            <IconBtn title="Next frame" onClick={() => stepFrame(1)}>
              <SkipForward size={16} />
            </IconBtn>

            <div className="mx-1 flex items-center gap-1">
              <IconBtn title="Slower" onClick={() => changeSpeed(-1)}>
                <Rewind size={16} />
              </IconBtn>
              <span className="text-xs font-mono w-8 text-center tabular-nums">
                {SPEEDS[rateIdx]}x
              </span>
              <IconBtn title="Faster" onClick={() => changeSpeed(1)}>
                <FastForward size={16} />
              </IconBtn>
            </div>

            <IconBtn title={muted ? 'Unmute' : 'Mute'} onClick={toggleMute}>
              {muted ? <VolumeX size={16} /> : <Volume2 size={16} />}
            </IconBtn>

            <button
              title={clipMode ? 'Exit clip mode' : 'Clip / export'}
              onClick={toggleClipMode}
              className={`p-1.5 rounded transition-colors ${
                clipMode
                  ? 'bg-[var(--accent)] text-white'
                  : 'text-[var(--text)] hover:bg-[var(--panel)]'
              }`}
            >
              <Scissors size={16} />
            </button>

            <div className="flex-1" />

            {/* Zoom control */}
            <div className="flex items-center rounded overflow-hidden border border-neutral-700">
              {ZOOMS.map((z, i) => (
                <button
                  key={z.label}
                  onClick={() => applyZoom(i)}
                  className={`px-2 py-1 text-[11px] font-mono transition-colors ${
                    i === zoomIdx
                      ? 'bg-[var(--accent)] text-white'
                      : 'text-[var(--text-dim)] hover:bg-[var(--panel)]'
                  }`}
                >
                  {z.label}
                </button>
              ))}
            </div>

            <IconBtn
              title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
              onClick={toggleFullscreen}
            >
              {isFullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
            </IconBtn>
          </div>
        )}

        {/* Clip action bar */}
        {!error && clipMode && (
          <div className="shrink-0 flex items-center gap-3 px-3 py-2 bg-[var(--accent)]/10 border-t border-[var(--accent)]/30 text-sm">
            <Scissors size={14} className="text-[var(--accent)] shrink-0" />
            <span className="text-[var(--text-dim)]">
              {selectionSeconds > 0
                ? `Selection: ${selectionSeconds}s`
                : 'Drag across the timeline to select a range to export'}
            </span>
            <div className="flex-1" />
            <button
              onClick={exportClip}
              disabled={selectionSeconds <= 0 || exporting}
              className="flex items-center gap-1.5 px-3 py-1 rounded bg-[var(--accent)] text-white text-xs font-medium disabled:opacity-40 disabled:cursor-not-allowed hover:opacity-90 transition-opacity"
            >
              {exporting ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
              {exporting ? 'Exporting…' : 'Export clip'}
            </button>
            <button
              onClick={toggleClipMode}
              className="px-3 py-1 rounded border border-neutral-600 text-xs text-[var(--text-dim)] hover:bg-[var(--panel)] transition-colors"
            >
              Cancel
            </button>
          </div>
        )}

        {/* Timeline */}
        {!error && segs.length > 0 && view.end > view.start && (
          <div className="shrink-0 px-3 pt-1 pb-3 bg-[var(--panel-2)]">
            <PlaybackTimeline
              segments={timelineSegs}
              liveEdgeMs={liveEdgeMs}
              viewStart={view.start}
              viewEnd={view.end}
              currentTime={effectiveCurrent}
              onSeek={seekTo}
              onScrubPreview={setPreviewMs}
              onZoomAt={zoomAt}
              onPan={panBy}
              mode={clipMode ? 'clip' : 'seek'}
              selection={clipMode ? selection : null}
              onSelectionChange={setSelection}
            />
          </div>
        )}
      </div>
    </div>
  )
}

function IconBtn({
  title,
  onClick,
  children,
}: {
  title: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      title={title}
      onClick={onClick}
      className="p-1.5 rounded text-[var(--text)] hover:bg-[var(--panel)] transition-colors"
    >
      {children}
    </button>
  )
}
