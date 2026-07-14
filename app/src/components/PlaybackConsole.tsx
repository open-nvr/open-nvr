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
import Hls from 'hls.js'
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
} from 'lucide-react'
import { apiService } from '../lib/apiService'
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

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [segs, setSegs] = useState<Seg[]>([])
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

  // ---- Load the day's segments ---------------------------------------------
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    apiService
      .getSegments(cameraId, date)
      .then((res: any) => {
        if (cancelled) return
        const parsed = parseSegments(res.data?.segments || [])

        setBase((res.data?.playback_base_url || '').replace(/\/$/, ''))
        setPath(res.data?.path || '')
        setSegs(parsed)

        if (parsed.length === 0) {
          setError('No recordings found for this day.')
          setLoading(false)
          return
        }

        // Day window = local calendar day that contains the first clip.
        const d = new Date(parsed[0].startMs)
        d.setHours(0, 0, 0, 0)
        const ds = d.getTime()
        const de = ds + 24 * 3600_000
        setDayStart(ds)
        setDayEnd(de)
        setView({ start: ds, end: de })
        setZoomIdx(0)
        setCurrentMs(parsed[0].startMs)
        windowStartRef.current = parsed[0].startMs
        setLoading(false)
      })
      .catch((e: any) => {
        if (cancelled) return
        setError(e?.message || 'Failed to load recordings')
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [cameraId, date])

  // Mirror segments to a ref for the poll loop.
  useEffect(() => {
    segsRef.current = segs
  }, [segs])

  // Live-follow: while the player is open on a still-recording day, re-fetch the
  // segment list so the timeline's red blocks grow as new footage lands — no
  // manual reload. Auto-stops once the newest footage is well in the past.
  useEffect(() => {
    if (loading || error) return
    let stopped = false
    const id = setInterval(async () => {
      const cur = segsRef.current
      const lastEnd = cur.length ? cur[cur.length - 1].endMs : 0
      // Static (past) day → nothing more will be written; stop polling.
      if (lastEnd && Date.now() - lastEnd > 15 * 60_000) return
      try {
        const res: any = await apiService.getSegments(cameraId, date)
        if (stopped) return
        const parsed = parseSegments(res.data?.segments || [])
        if (parsed.length && !sameSegs(segsRef.current, parsed)) setSegs(parsed)
      } catch {
        /* transient — try again next tick */
      }
    }, 10_000)
    return () => {
      stopped = true
      clearInterval(id)
    }
  }, [loading, error, cameraId, date])

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
    async (clip: Seg, offsetSec: number, play: boolean) => {
      const el = videoRef.current
      if (!el) return
      const token = ++loadTokenRef.current

      teardownHls()

      windowStartRef.current = clip.startMs
      loadedClipRef.current = { startMs: clip.startMs, endMs: clip.endMs }
      setCurrentMs(clip.startMs + offsetSec * 1000)
      setBuffering(true)

      let manifestUrl: string
      try {
        const res: any = await apiService.createHlsPlaybackSession({
          camera_id: cameraId,
          start: clip.startIso,
          end: new Date(clip.endMs).toISOString(),
        })
        if (token !== loadTokenRef.current) return // superseded by a newer load
        manifestUrl = res.data?.manifest_url
        sessionIdRef.current = res.data?.session_id || null
        if (!manifestUrl) throw new Error('No manifest returned')
      } catch {
        if (token === loadTokenRef.current) {
          setBuffering(false)
          setError('Failed to start playback for this clip.')
        }
        return
      }

      const startAtOffset = () => {
        if (token !== loadTokenRef.current) return
        el.playbackRate = SPEEDS[rateIdx]
        el.muted = muted
        if (offsetSec > 0.05) {
          try {
            el.currentTime = offsetSec
          } catch {
            /* seekable range not ready; starts at head */
          }
        }
        if (play) el.play().catch(() => {})
        setBuffering(false)
      }

      if (Hls.isSupported()) {
        const hls = new Hls({
          enableWorker: true,
          lowLatencyMode: false,
          backBufferLength: 30,
          maxBufferLength: 60,
          maxMaxBufferLength: 120,
        })
        hlsRef.current = hls
        hls.loadSource(manifestUrl)
        hls.attachMedia(el)
        hls.on(Hls.Events.MANIFEST_PARSED, startAtOffset)
        hls.on(Hls.Events.ERROR, (_evt, data) => {
          if (!data.fatal) return
          if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError()
          else if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad()
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
      // The current clip may have grown since we loaded it (live recording) —
      // reload it from where we stopped to play into the newly-written tail.
      const grown = segs.find((s) => s.startMs <= after && after < s.endMs - 500)
      if (grown) {
        loadClip(grown, Math.max(0, (after - grown.startMs) / 1000), true)
        return
      }
      // Otherwise advance to the next clip (skips the grey gap).
      const next = segs.find((s) => s.startMs >= after - 500)
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
      const url = `${base}/get?path=${path}&start=${encodeURIComponent(startIso)}&duration=${durationSec}`
      const res = await fetch(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const href = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = href
      a.download = `${cameraName.replace(/\s+/g, '-')}_${stamp(selection.inMs)}.mp4`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(href)
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
              {(loading || buffering) && (
                <div className="absolute inset-0 flex items-center justify-center bg-black/30 pointer-events-none">
                  <Loader2 size={40} className="animate-spin text-[var(--accent)]" />
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
              viewStart={view.start}
              viewEnd={view.end}
              currentTime={effectiveCurrent}
              onSeek={seekTo}
              onScrubPreview={setPreviewMs}
              onZoomAt={zoomAt}
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
