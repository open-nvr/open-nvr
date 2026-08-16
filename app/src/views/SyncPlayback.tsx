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
import {
  Play,
  Pause,
  Square,
  Rewind,
  FastForward,
  Volume2,
  VolumeX,
  Maximize2,
  Minimize2,
  MonitorPlay,
  CalendarDays,
  ChevronDown,
  PanelRightClose,
  PanelRightOpen,
  Check,
  Loader2,
  Film,
  Video,
} from 'lucide-react'
import { warmHls } from '../lib/loadHls'
import { useRecordingsByDate, useSegmentsForCameras } from '../lib/queries'
import { localDayEnd, localDayStart, todayLocalKey } from '../lib/time'
import { useSnackbar } from '../components/Snackbar'
import { RecordingCalendar } from '../components/RecordingCalendar'
import { MultiCamTimeline, type TimelineRow } from '../components/MultiCamTimeline'
import { SyncPlaybackTile } from '../components/SyncPlaybackTile'
import type { TimelineSegment } from '../components/PlaybackTimeline'

interface DailyRecording {
  date: string
  total_duration: number
  segment_count: number
  first_start: string
  playback_url: string | null
}

interface OverviewCamera {
  camera_id: number
  camera_name: string
  path: string
  recording_count: number
  total_duration: number
  recordings: DailyRecording[]
}

interface RawSegment {
  start: string
  duration: number
  playback_url: string
}

const ZOOMS: { label: string; span: number }[] = [
  { label: '24h', span: 24 * 3600_000 },
  { label: '6h', span: 6 * 3600_000 },
  { label: '1h', span: 3600_000 },
  { label: '10m', span: 600_000 },
]

const SPEEDS = [0.25, 0.5, 1, 2, 4, 8, 16]
const RATE_1X = 2
/** Grid limit — also keeps concurrent HLS sessions within the server cap. */
const MAX_TILES = 4

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

function parseSegments(raw: RawSegment[]): TimelineSegment[] {
  return raw
    .map((r) => {
      const startMs = Date.parse(r.start)
      return { startMs, endMs: startMs + (r.duration || 0) * 1000 }
    })
    .filter((s) => Number.isFinite(s.startMs))
    .sort((a, b) => a.startMs - b.startMs)
}

// Day windows are LOCAL days (midnight -> midnight in the browser's zone),
// matching the API's local-day grouping — see lib/time.ts.

function formatDuration(seconds: number) {
  const hours = Math.floor(seconds / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  return hours > 0 ? `${hours}h ${mins}m` : `${mins}m`
}

function formatDateLong(date: string) {
  const [y, m, d] = date.split('-').map(Number)
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    weekday: 'short',
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

/**
 * CP-Plus-style synchronized playback: a grid of cameras playing the same
 * wall-clock instant, a calendar marking days that have footage, a camera
 * checklist, and one multi-track timeline driving every tile.
 */
export function SyncPlayback() {
  const { showError } = useSnackbar()
  const stageRef = useRef<HTMLDivElement>(null)

  // Download the hls.js chunk while the overview request is in flight — the
  // tiles will need it the moment segments land.
  useEffect(() => {
    warmHls()
  }, [])

  // ---- Overview: which cameras have recordings on which days ---------------
  // react-query: cached across navigations (Dashboard -> Recordings renders
  // instantly from cache), deduped across concurrent mounts.
  const overviewQuery = useRecordingsByDate()
  const overview = (overviewQuery.data?.cameras as OverviewCamera[] | undefined) ?? null
  const overviewLoading = overviewQuery.isPending
  const overviewError = overviewQuery.error
    ? (overviewQuery.error as any)?.message || 'Failed to load recordings'
    : null
  const mediamtxAvailable = overviewQuery.data?.mediamtx_available !== false
  const loadOverview = overviewQuery.refetch

  // ---- Selection -----------------------------------------------------------
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [calOpen, setCalOpen] = useState(true)
  const [panelOpen, setPanelOpen] = useState(() => {
    try {
      return localStorage.getItem('opennvr.syncplayback.panel') !== '0'
    } catch {
      return true
    }
  })

  const togglePanel = () =>
    setPanelOpen((o) => {
      try {
        localStorage.setItem('opennvr.syncplayback.panel', o ? '0' : '1')
      } catch {
        /* storage unavailable: keep in-memory state */
      }
      return !o
    })

  // ---- Transport (the shared master clock) ---------------------------------
  const [masterMs, setMasterMs] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [rateIdx, setRateIdx] = useState(RATE_1X)
  const [muted, setMuted] = useState(false)
  const [view, setView] = useState<{ start: number; end: number }>({ start: 0, end: 0 })
  const [zoomIdx, setZoomIdx] = useState(0)
  const [previewMs, setPreviewMs] = useState<number | null>(null)
  const [isFullscreen, setIsFullscreen] = useState(false)

  const masterMsRef = useRef(masterMs)
  useEffect(() => {
    masterMsRef.current = masterMs
  }, [masterMs])

  const dayStart = selectedDate ? localDayStart(selectedDate) : 0
  const dayEnd = selectedDate ? localDayEnd(selectedDate) : 0
  const dayBoundsRef = useRef({ start: dayStart, end: dayEnd })
  dayBoundsRef.current = { start: dayStart, end: dayEnd }

  // Marks masterMs/view as initialized for the current day.
  const dateInitRef = useRef<string | null>(null)

  // Default selection once the overview lands: latest day with footage, and
  // the first few cameras that recorded that day.
  useEffect(() => {
    if (!overview || selectedDate) return
    let latest: string | null = null
    for (const c of overview) for (const r of c.recordings) if (!latest || r.date > latest) latest = r.date
    if (!latest) return
    const ids = overview.filter((c) => c.recordings.some((r) => r.date === latest)).map((c) => c.camera_id)
    setSelectedDate(latest)
    setSelectedIds(ids.slice(0, MAX_TILES))
    setActiveId(ids[0] ?? null)
  }, [overview, selectedDate])

  // ---- Derived: calendar marks, per-date availability ----------------------
  const markedDates = useMemo(() => {
    const s = new Set<string>()
    for (const c of overview || []) for (const r of c.recordings) s.add(r.date)
    return s
  }, [overview])

  // O(1) lookups instead of scanning every camera's recordings array on
  // every render (the checklist used to do that 4x/second on clock ticks).
  const recByCamDate = useMemo(() => {
    const m = new Map<number, Map<string, DailyRecording>>()
    for (const c of overview || []) {
      const dm = new Map<string, DailyRecording>()
      for (const r of c.recordings) dm.set(r.date, r)
      m.set(c.camera_id, dm)
    }
    return m
  }, [overview])

  // ---- Fetch segments for the selected cameras/day -------------------------
  // One react-query per camera+day. Polling (today only) is handled by the
  // hook; structural sharing keeps unchanged responses referentially stable,
  // so a poll tick that changes nothing invalidates nothing downstream —
  // the old hand-rolled loop replaced every array on every tick and re-fired
  // every tile's follow loop.
  const segQueries = useSegmentsForCameras(selectedIds, selectedDate, { poll: true })

  // Fixed-length deps (MAX_TILES = 4) keep the hook contract while still
  // recomputing only when a query's (structurally shared) data changes.
  const segData0 = segQueries[0]?.data
  const segData1 = segQueries[1]?.data
  const segData2 = segQueries[2]?.data
  const segData3 = segQueries[3]?.data

  const segsByCam = useMemo(() => {
    const datas = [segData0, segData1, segData2, segData3]
    const out: Record<number, TimelineSegment[]> = {}
    selectedIds.forEach((id, i) => {
      out[id] = parseSegments((datas[i]?.segments as RawSegment[]) || [])
    })
    return out
  }, [selectedIds, segData0, segData1, segData2, segData3])

  const liveEdges = useMemo(() => {
    const datas = [segData0, segData1, segData2, segData3]
    const out: Record<number, number | null> = {}
    selectedIds.forEach((id, i) => {
      const raw = datas[i]?.live_edge_start
      const edge = raw ? Date.parse(raw) : NaN
      out[id] = Number.isFinite(edge) ? edge : null
    })
    return out
  }, [selectedIds, segData0, segData1, segData2, segData3])

  const segsLoading =
    selectedIds.length > 0 && segQueries.some((q) => q.isPending)

  // Union of all selected cameras' footage — used for snapping, gap skipping
  // and picking the day's starting instant.
  const unionSegs = useMemo(() => {
    const all = Object.values(segsByCam).flat()
    return all.sort((a, b) => a.startMs - b.startMs)
  }, [segsByCam])
  const unionRef = useRef(unionSegs)
  useEffect(() => {
    unionRef.current = unionSegs
  }, [unionSegs])

  // Initialize the playhead and window once per selected day.
  useEffect(() => {
    if (segsLoading || !selectedDate) return
    if (dateInitRef.current === selectedDate) return
    if (Object.keys(segsByCam).length === 0) return
    dateInitRef.current = selectedDate
    const first = unionSegs.length ? unionSegs[0].startMs : dayStart
    setMasterMs(first)
    setView({ start: dayStart, end: dayEnd })
    setZoomIdx(0)
    setPlaying(true)
  }, [segsLoading, selectedDate, segsByCam, unionSegs, dayStart, dayEnd])

  // ---- Master clock: advances wall-clock time, skipping global gaps --------
  useEffect(() => {
    if (!playing) return
    let last = performance.now()
    const id = setInterval(() => {
      const now = performance.now()
      const delta = (now - last) * SPEEDS[rateIdx]
      last = now
      let next = masterMsRef.current + delta
      const u = unionRef.current
      // Dead air where NO selected camera has footage is skipped to the next
      // recorded instant instead of played through in real time.
      if (!u.some((s) => next >= s.startMs && next < s.endMs)) {
        const nx = u.find((s) => s.startMs > next)
        if (nx) next = nx.startMs
        else {
          setPlaying(false)
          return
        }
      }
      // Never chase into footage still being written (or the end of the day).
      const cap = Math.min(Date.now() - 60_000, dayBoundsRef.current.end - 1)
      if (next >= cap) {
        setPlaying(false)
        next = Math.min(masterMsRef.current, cap)
      }
      setMasterMs(next)
    }, 250)
    return () => clearInterval(id)
  }, [playing, rateIdx])

  // Keep the playhead inside the visible window while playing (auto-follow).
  useEffect(() => {
    if (previewMs != null) return
    if (masterMs < view.start || masterMs > view.end) {
      const span = view.end - view.start
      if (span <= 0) return
      let start = clamp(masterMs - span / 2, dayStart, dayEnd - span)
      if (span >= dayEnd - dayStart) start = dayStart
      setView({ start, end: start + span })
    }
  }, [masterMs, previewMs, view.start, view.end, dayStart, dayEnd])

  // Fullscreen tracking.
  useEffect(() => {
    const onFs = () => setIsFullscreen(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', onFs)
    return () => document.removeEventListener('fullscreenchange', onFs)
  }, [])

  // ---- Handlers ------------------------------------------------------------
  const selectDate = (date: string) => {
    if (date === selectedDate) return
    if (!markedDates.has(date)) return // nothing recorded that day
    const avail = (overview || []).filter((c) => c.recordings.some((r) => r.date === date)).map((c) => c.camera_id)
    setSelectedDate(date)
    dateInitRef.current = null
    setPlaying(false)
    setSelectedIds((prev) => {
      const kept = prev.filter((id) => avail.includes(id))
      return kept.length ? kept : avail.slice(0, 1)
    })
  }

  const toggleCamera = useCallback(
    (id: number) => {
      setSelectedIds((prev) => {
        if (prev.includes(id)) {
          const next = prev.filter((x) => x !== id)
          setActiveId((a) => (a === id ? next[0] ?? null : a))
          return next
        }
        if (prev.length >= MAX_TILES) {
          showError(`Up to ${MAX_TILES} cameras can play together`)
          return prev
        }
        setActiveId((a) => (a == null ? id : a))
        return [...prev, id]
      })
    },
    [showError]
  )

  const seekTo = (ms: number) => {
    setMasterMs(clamp(ms, dayStart, dayEnd - 1))
  }

  const togglePlay = () => setPlaying((p) => !p)

  const stop = () => {
    setPlaying(false)
    setMasterMs(unionSegs.length ? unionSegs[0].startMs : dayStart)
  }

  const changeSpeed = (dir: 1 | -1) => setRateIdx((i) => clamp(i + dir, 0, SPEEDS.length - 1))

  const applyZoom = (idx: number) => {
    const span = ZOOMS[idx].span
    setZoomIdx(idx)
    const center = previewMs ?? masterMs
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

  const zoomAt = useCallback(
    (anchorMs: number, factor: number) => {
      setZoomIdx(-1)
      setView((v) => {
        const span = v.end - v.start
        const total = dayEnd - dayStart
        const MIN_SPAN = 30_000
        const newSpan = clamp(span * factor, MIN_SPAN, total)
        if (newSpan >= total) return { start: dayStart, end: dayEnd }
        const rel = span > 0 ? (anchorMs - v.start) / span : 0.5
        const start = clamp(anchorMs - rel * newSpan, dayStart, dayEnd - newSpan)
        return { start, end: start + newSpan }
      })
    },
    [dayStart, dayEnd]
  )

  const toggleFullscreen = () => {
    if (document.fullscreenElement) document.exitFullscreen?.()
    else stageRef.current?.requestFullscreen?.()
  }

  // ---- Layout derivations --------------------------------------------------
  const selectedCams = useMemo(
    () => selectedIds.map((id) => (overview || []).find((c) => c.camera_id === id)).filter(Boolean) as OverviewCamera[],
    [selectedIds, overview]
  )
  const cols = Math.max(1, Math.ceil(Math.sqrt(selectedCams.length)))
  const rows = Math.max(1, Math.ceil(selectedCams.length / cols))

  const timelineRows: TimelineRow[] = useMemo(
    () =>
      selectedCams.map((c) => ({
        id: c.camera_id,
        name: c.camera_name,
        segments: segsByCam[c.camera_id] || [],
        liveEdgeMs: liveEdges[c.camera_id] ?? null,
      })),
    [selectedCams, segsByCam, liveEdges]
  )

  const effectiveCurrent = previewMs ?? masterMs
  const activeCam = selectedCams.find((c) => c.camera_id === activeId)

  // ---- Render --------------------------------------------------------------
  // While the overview loads, the page frame renders immediately with
  // skeletons in the data slots (no full-page spinner: first useful paint
  // must not wait on the slowest API call).
  if (!overviewLoading && (overviewError || !overview || overview.length === 0)) {
    return (
      <div className="space-y-4">
        <PageTitle />
        <div className="bg-[var(--panel-2)] border border-[var(--border)] p-12 text-center">
          <Film size={48} className="mx-auto mb-4 opacity-30" />
          <p className="text-[var(--text-dim)]">{overviewError || 'No recordings found'}</p>
          <p className="text-sm text-[var(--text-dim)] mt-1">
            {overviewError ? 'Try refreshing.' : 'Recordings will appear here once cameras start recording.'}
          </p>
          <button onClick={() => loadOverview()} className="btn-primary btn mt-4">
            Refresh
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3 lg:flex-row lg:h-[calc(100vh-6rem)]">
      {/* Stage: header + video grid + transport + timeline */}
      <div
        ref={stageRef}
        className={`flex-1 min-w-0 flex flex-col gap-2 min-h-0 ${isFullscreen ? 'bg-[var(--bg)] p-3' : ''}`}
      >
        <div className="shrink-0 flex items-center gap-3 flex-wrap">
          <PageTitle />
          {selectedDate && (
            <span className="inline-flex items-center gap-1.5 text-sm text-[var(--text-dim)]">
              <CalendarDays size={14} className="text-[var(--accent)]" />
              {formatDateLong(selectedDate)}
            </span>
          )}
          <span className="text-sm text-[var(--text-dim)]">
            {selectedCams.length} camera{selectedCams.length !== 1 ? 's' : ''}
          </span>
          {!mediamtxAvailable && (
            <span className="text-sm text-amber-400">Playback server offline — playback unavailable</span>
          )}
          {!isFullscreen && (
            <button
              onClick={togglePanel}
              title={panelOpen ? 'Hide calendar & camera panel' : 'Show calendar & camera panel'}
              className="ml-auto p-1 text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel-2)]"
            >
              {panelOpen ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}
            </button>
          )}
        </div>

        {/* Video grid */}
        <div
          className="flex-1 min-h-0 grid gap-1"
          style={{
            gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
            gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))`,
          }}
        >
          {overviewLoading ? (
            <div className="border border-[var(--border)] bg-[var(--panel-2)] animate-pulse flex flex-col items-center justify-center gap-2 py-16 text-[var(--text-dim)]">
              <Loader2 size={22} className="animate-spin text-[var(--accent)]" />
              <span className="text-sm">Loading recordings…</span>
            </div>
          ) : selectedCams.length === 0 ? (
            <div className="border border-dashed border-[var(--border)] flex flex-col items-center justify-center gap-2 py-16 text-[var(--text-dim)]">
              <Video size={28} className="opacity-50" />
              <span className="text-sm">Select cameras from the panel to start playback</span>
            </div>
          ) : (
            selectedCams.map((cam) => (
              <SyncPlaybackTile
                key={`${cam.camera_id}:${selectedDate}`}
                cameraId={cam.camera_id}
                cameraName={cam.camera_name}
                segments={segsByCam[cam.camera_id] || []}
                liveEdgeMs={liveEdges[cam.camera_id] ?? null}
                masterMs={masterMs}
                playing={playing}
                rate={SPEEDS[rateIdx]}
                muted={muted || cam.camera_id !== activeId}
                active={cam.camera_id === activeId}
                onActivate={() => setActiveId(cam.camera_id)}
              />
            ))
          )}
        </div>

        {/* Transport controls */}
        <div className="shrink-0 flex items-center gap-0.5 px-1.5 py-0.5 bg-[var(--panel-2)] border border-[var(--border)] flex-wrap">
          <IconBtn title={playing ? 'Pause' : 'Play'} onClick={togglePlay}>
            {playing ? <Pause size={16} /> : <Play size={16} />}
          </IconBtn>
          <IconBtn title="Stop (back to first footage)" onClick={stop}>
            <Square size={13} />
          </IconBtn>

          <div className="mx-1 flex items-center gap-0.5">
            <IconBtn title="Slower" onClick={() => changeSpeed(-1)}>
              <Rewind size={14} />
            </IconBtn>
            <span className="text-[11px] font-mono w-7 text-center tabular-nums">{SPEEDS[rateIdx]}x</span>
            <IconBtn title="Faster" onClick={() => changeSpeed(1)}>
              <FastForward size={14} />
            </IconBtn>
          </div>

          <IconBtn title={muted ? 'Unmute selected camera' : 'Mute'} onClick={() => setMuted((m) => !m)}>
            {muted ? <VolumeX size={14} /> : <Volume2 size={14} />}
          </IconBtn>
          {activeCam && (
            <span className="text-[11px] text-[var(--text-dim)] truncate max-w-32" title={`Audio: ${activeCam.camera_name}`}>
              {activeCam.camera_name}
            </span>
          )}

          <div className="flex-1" />

          <div className="flex items-center overflow-hidden border border-[var(--border)]">
            {ZOOMS.map((z, i) => (
              <button
                key={z.label}
                onClick={() => applyZoom(i)}
                className={`px-1.5 py-0.5 text-[10px] font-mono transition-colors ${
                  i === zoomIdx
                    ? 'bg-[var(--accent)] text-white'
                    : 'text-[var(--text-dim)] hover:bg-[var(--panel)]'
                }`}
              >
                {z.label}
              </button>
            ))}
          </div>

          <IconBtn title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'} onClick={toggleFullscreen}>
            {isFullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </IconBtn>
        </div>

        {/* Multi-camera timeline */}
        {timelineRows.length > 0 && view.end > view.start && (
          <div className="shrink-0 px-2 pt-0.5 pb-1 bg-[var(--panel-2)] border border-[var(--border)]">
            {segsLoading ? (
              <div className="h-16 flex items-center justify-center text-sm text-[var(--text-dim)]">
                <Loader2 size={16} className="animate-spin mr-2 text-[var(--accent)]" /> Loading timeline…
              </div>
            ) : (
              <MultiCamTimeline
                rows={timelineRows}
                viewStart={view.start}
                viewEnd={view.end}
                currentTime={effectiveCurrent}
                onSeek={seekTo}
                onScrubPreview={setPreviewMs}
                onZoomAt={zoomAt}
                onPan={panBy}
                activeId={activeId}
                onRowClick={setActiveId}
              />
            )}
          </div>
        )}
      </div>

      {/* Side panel: calendar + camera checklist */}
      {panelOpen && (
      <aside className="w-full lg:w-60 xl:w-64 shrink-0 flex flex-col gap-2 min-h-0">
        {/* Date picker */}
        <div className="bg-[var(--panel-2)] border border-[var(--border)]">
          <button
            onClick={() => setCalOpen((o) => !o)}
            className="w-full flex items-center gap-2 px-2.5 py-1.5 text-sm hover:bg-[var(--panel)]"
            aria-expanded={calOpen}
          >
            <CalendarDays size={15} className="text-[var(--accent)]" />
            <span className="font-medium">{selectedDate ? formatDateLong(selectedDate) : 'Pick a date'}</span>
            <ChevronDown size={15} className={`ml-auto transition-transform ${calOpen ? '' : '-rotate-90'}`} />
          </button>
          {calOpen && (
            <div className="px-2.5 pb-2.5 border-t border-[var(--border)] pt-1.5">
              <RecordingCalendar selected={selectedDate} onSelect={selectDate} markedDates={markedDates} />
            </div>
          )}
        </div>

        {/* Camera checklist */}
        <div className="bg-[var(--panel-2)] border border-[var(--border)] flex flex-col min-h-0 lg:flex-1">
          <div className="shrink-0 flex items-center gap-2 px-2.5 py-1.5 border-b border-[var(--border)]">
            <Video size={15} className="text-[var(--accent)]" />
            <span className="text-sm font-medium">Cameras</span>
            <span className="ml-auto text-xs text-[var(--text-dim)]">
              {selectedIds.length}/{MAX_TILES}
            </span>
          </div>
          <CameraChecklist
            overview={overview}
            overviewLoading={overviewLoading}
            recByCamDate={recByCamDate}
            selectedDate={selectedDate}
            selectedIds={selectedIds}
            activeId={activeId}
            onToggle={toggleCamera}
          />
        </div>
      </aside>
      )}
    </div>
  )
}

function PageTitle() {
  return (
    <h1 className="text-lg font-semibold flex items-center gap-2">
      <MonitorPlay size={20} className="text-[var(--accent)]" />
      Playback
    </h1>
  )
}

/**
 * Camera checkbox list. Memoized so master-clock ticks (which re-render the
 * parent 4x/second) never touch it — its props only change on data load,
 * date/selection change, or toggle.
 */
const CameraChecklist = memo(function CameraChecklist({
  overview,
  overviewLoading,
  recByCamDate,
  selectedDate,
  selectedIds,
  activeId,
  onToggle,
}: {
  overview: OverviewCamera[] | null
  overviewLoading: boolean
  recByCamDate: Map<number, Map<string, DailyRecording>>
  selectedDate: string | null
  selectedIds: number[]
  activeId: number | null
  onToggle: (id: number) => void
}) {
  return (
    <div className="overflow-y-auto sidebar-scroll max-h-64 lg:max-h-none">
      {overviewLoading &&
        Array.from({ length: 5 }, (_, i) => (
          <div key={i} className="flex items-center gap-2 px-2.5 py-1.5 animate-pulse">
            <span className="w-4 h-4 bg-[var(--panel)]" />
            <span className="h-3 flex-1 bg-[var(--panel)]" />
          </div>
        ))}
      {(overview || []).map((cam) => {
        const rec = selectedDate ? recByCamDate.get(cam.camera_id)?.get(selectedDate) : undefined
        const checked = selectedIds.includes(cam.camera_id)
        const disabled = !rec && !checked
        return (
          <button
            key={cam.camera_id}
            onClick={() => !disabled && onToggle(cam.camera_id)}
            disabled={disabled}
            title={rec ? `${formatDuration(rec.total_duration)} recorded` : 'No recordings on this date'}
            className={`group w-full flex items-center gap-2 px-2.5 py-1.5 text-[13px] text-left transition-colors border-l-2 focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent)] ${
              cam.camera_id === activeId ? 'border-l-[var(--accent)] text-[var(--accent)]' : 'border-l-transparent'
            } ${checked ? 'bg-[var(--accent)]/10' : ''} ${
              disabled ? 'opacity-40 cursor-not-allowed' : 'hover:bg-[var(--panel)]'
            }`}
          >
            <span
              className={`shrink-0 w-4 h-4 border flex items-center justify-center transition-colors ${
                checked
                  ? 'bg-[var(--accent)] border-[var(--accent)]'
                  : `border-[var(--border)] ${disabled ? '' : 'group-hover:border-[var(--accent)]/60'}`
              }`}
            >
              {checked && <Check size={12} className="text-white" />}
            </span>
            <span className="truncate flex-1">{cam.camera_name}</span>
            <span className="shrink-0 text-[10px] font-mono text-[var(--text-dim)]">
              {rec ? formatDuration(rec.total_duration) : '—'}
            </span>
          </button>
        )
      })}
    </div>
  )
})

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
      className="p-1 text-[var(--text)] hover:bg-[var(--panel)] transition-colors"
    >
      {children}
    </button>
  )
}
