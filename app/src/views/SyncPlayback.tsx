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
import { useSearchParams } from 'react-router-dom'
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
  Scissors,
  Download,
  Upload,
  MousePointerClick,
} from 'lucide-react'
import { apiService } from '../lib/apiService'
import {
  MAX_BUFFERED_CLIP_SECONDS,
  canPickFiles,
  fetchClip,
  nativeDownload,
  pickDestination,
  saveBlob,
  type ClipSink,
} from '../lib/clipDownload'
import { ClipDownloadPanel, clipJobRunning, type ClipJob } from '../components/ClipDownloadPanel'
import { useAuth } from '../auth/AuthContext'
import { useConfirm } from '../components/ui/ConfirmDialog'
import { warmHls } from '../lib/loadHls'
import { useCameraAspects, useRecordingsByDate, useSegmentsForCameras } from '../lib/queries'
import { localDateKey, localDayEnd, localDayStart, todayLocalKey } from '../lib/time'
import { useSnackbar } from '../components/Snackbar'
import { useTranslation, useDateFormat, type DateFormatters } from '../i18n'
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

interface CloudUploadStatus {
  queue_size: number
  worker_running: boolean
  configured?: boolean
  stats?: { completed_total?: number; failed_total?: number }
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

/** Clip export: the server streams at most 4 h per clip. */
const MAX_EXPORT_MS = 4 * 3600_000
const EXPORT_PRESETS: { label: string; ms: number }[] = [
  { label: '1m', ms: 60_000 },
  { label: '5m', ms: 300_000 },
  { label: '15m', ms: 900_000 },
  { label: '1h', ms: 3600_000 },
]

const SPEEDS = [0.25, 0.5, 1, 2, 4, 8, 16]
const RATE_1X = 2
/** Grid limit — also keeps concurrent HLS sessions within the server cap. */
const MAX_TILES = 4

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

// How far before a deep-linked instant the playhead lands (see the
// per-day initialiser): long enough to cover the detect stream's latency
// and a keyframe-only idle decode waking up a GOP late.
const DEEP_LINK_LEAD_MS = 3000

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

function formatClipLength(ms: number) {
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m${s % 60 ? ` ${s % 60}s` : ''}`
}

function formatDateLong(date: string, fmt: DateFormatters) {
  const [y, m, d] = date.split('-').map(Number)
  return fmt.date(new Date(y, m - 1, d), {
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
  const fmt = useDateFormat()
  const { t } = useTranslation()
  const { showError, showSuccess } = useSnackbar()
  const { user } = useAuth()
  const confirm = useConfirm()
  const stageRef = useRef<HTMLDivElement>(null)
  // Per-camera display-aspect settings for the grid tiles (#354).
  const cameraAspects = useCameraAspects()

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
    ? ((overviewQuery.error as any)?.message === 'API_NETWORK_ERROR'
      ? t('dashboard.failedRecordings')
      : (overviewQuery.error as any)?.message || t('dashboard.failedRecordings'))
    : null
  const mediamtxAvailable = overviewQuery.data?.mediamtx_available !== false
  const loadOverview = overviewQuery.refetch

  // ---- Deep link -----------------------------------------------------------
  //
  // ``/playback/sync?camera=3&at=2026-09-18T14:32:07Z`` opens on that camera,
  // that day, with the playhead at that instant. Search results, alarms and
  // the app pages all end at a camera and a moment; without this they end at
  // a timestamp the operator has to find by hand, which is most of the work.
  //
  // Read ONCE, in the state initialisers: a later read would fight the
  // day-selection effect below, and an operator who then scrubs somewhere
  // else must not be yanked back by their own URL.
  const [searchParams] = useSearchParams()
  const deepLink = useRef<{ camera: number | null; atMs: number | null } | null>(null)
  if (deepLink.current === null) {
    const rawCam = Number(searchParams.get('camera'))
    const rawAt = Date.parse(searchParams.get('at') || '')
    deepLink.current = {
      camera: Number.isFinite(rawCam) && rawCam > 0 ? rawCam : null,
      atMs: Number.isFinite(rawAt) ? rawAt : null,
    }
  }
  const link = deepLink.current
  //: Consumed by the per-day initialiser below, then cleared — so it seeks
  //: once rather than every time the day's segments reload.
  const pendingSeekMs = useRef<number | null>(link.atMs)
  //: A deep link whose instant has no footage on its camera: the playhead
  //: goes to the nearest later recording, and the operator is TOLD, because
  //: the alternative (what the master clock does on its own in dead air) is
  //: a silent jump to footage of some other moment, which reads as "the
  //: subject is not there".
  const [deepLinkMiss, setDeepLinkMiss] = useState<{ askedMs: number; nearestMs: number | null } | null>(null)

  // ---- Selection -----------------------------------------------------------
  const [selectedDate, setSelectedDate] = useState<string | null>(
    link.atMs != null ? localDateKey(link.atMs) : null,
  )
  const [selectedIds, setSelectedIds] = useState<number[]>(
    link.camera != null ? [link.camera] : [],
  )
  const [activeId, setActiveId] = useState<number | null>(link.camera)
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

  // ---- Clip export ---------------------------------------------------------
  // Export mode turns a timeline drag into a range; Download fetches one MP4
  // per chosen camera through the ticketed export route (streamed to disk).
  const [exportMode, setExportMode] = useState(false)
  const [exportSel, setExportSel] = useState<{ startMs: number; endMs: number } | null>(null)
  const [exportCamIds, setExportCamIds] = useState<number[]>([])
  // Whether the operator has dragged a range yet (the hint changes once they have).
  const [exportDrawn, setExportDrawn] = useState(false)
  // Downloads in flight / just finished, shown in the panel; abort handles by job id.
  const [clipJobs, setClipJobs] = useState<ClipJob[]>([])
  const clipAborts = useRef(new Map<string, AbortController>())

  // ---- Cloud upload (superusers) -------------------------------------------
  // Queue one camera's day for the configured cloud storage. The queue is
  // watched only after something was queued, until it drains.
  const [uploadingKey, setUploadingKey] = useState<string | null>(null)
  const [cloudStatus, setCloudStatus] = useState<CloudUploadStatus | null>(null)
  const [cloudWatch, setCloudWatch] = useState(false)

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
    // A link that named a camera but no instant still means THAT camera —
    // landing on the fleet's latest day with five other tiles up is not
    // what "open this camera" asked for.
    setSelectedIds(link.camera != null ? [link.camera] : ids.slice(0, MAX_TILES))
    setActiveId(link.camera ?? ids[0] ?? null)
  }, [overview, selectedDate, link.camera])

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

  // Footage recorded on the selected day: total seconds + cameras that have any.
  const dayTotal = useMemo(() => {
    if (!selectedDate) return { seconds: 0, cams: 0 }
    let seconds = 0
    let cams = 0
    for (const c of overview || []) {
      const rec = recByCamDate.get(c.camera_id)?.get(selectedDate)
      if (rec) {
        seconds += rec.total_duration || 0
        cams++
      }
    }
    return { seconds, cams }
  }, [overview, recByCamDate, selectedDate])

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
    // A deep link asked for an instant: honour it, clamped into the day, and
    // spend it — reloads of this day's segments must not drag the playhead
    // back to where the link pointed an hour ago.
    const asked = pendingSeekMs.current
    pendingSeekMs.current = null
    let target = first
    if (asked != null && asked >= dayStart && asked < dayEnd) {
      // The linked camera's own footage decides, not the union: another
      // camera recording at that instant is no help in finding this one.
      const linkCam = deepLink.current?.camera ?? null
      const own = linkCam != null ? segsByCam[linkCam] : undefined
      const segs = own && own.length ? own : unionSegs
      const hit = segs.find((s) => asked >= s.startMs && asked < s.endMs)
      if (hit) {
        // A little before the instant, inside the same segment: an event's
        // start is stamped when the detector first saw the object, which is
        // after it entered the frame (stream latency, and the idle decode
        // that wakes up on a keyframe), so the entry is on screen too.
        target = Math.max(hit.startMs, asked - DEEP_LINK_LEAD_MS)
      } else {
        const nx = segs.find((s) => s.startMs > asked)
        setDeepLinkMiss({ askedMs: asked, nearestMs: nx ? nx.startMs : null })
        if (nx) target = nx.startMs
      }
    }
    setMasterMs(target)
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

  // Cloud upload progress, polled while a queued upload is moving.
  useEffect(() => {
    if (!cloudWatch) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const poll = async () => {
      try {
        const { data } = await apiService.getCloudUploadStatus()
        if (stopped) return
        setCloudStatus(data)
        if (!data || data.configured === false || (!data.worker_running && !data.queue_size)) {
          setCloudWatch(false)
          return
        }
      } catch {
        // Informational only: a failed status read never blocks playback.
      }
      if (!stopped) timer = setTimeout(poll, 3000)
    }
    poll()
    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
    }
  }, [cloudWatch])

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
    setDeepLinkMiss(null)
    setPlaying(false)
    setExportMode(false)
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

  const uploadDay = useCallback(
    async (cam: OverviewCamera, date: string) => {
      // A whole day of footage leaves the box: say so first.
      const ok = await confirm({
        title: t('playback.confirmUploadTitle'),
        message: t('playback.confirmUploadMessage', { camera: cam.camera_name || `Camera ${cam.camera_id}`, date }),
        confirmLabel: t('playback.upload'),
      })
      if (!ok) return
      const key = `${cam.camera_id}:${date}`
      setUploadingKey(key)
      try {
        const { data } = await apiService.queueCloudUploadForDay(cam.camera_id, date)
        const queued = data?.queued ?? 0
        const skipped = data?.skipped_missing ?? 0
        showSuccess(`Queued ${queued} file(s) for cloud upload${skipped ? ` (${skipped} missing skipped)` : ''}`)
        setCloudWatch(true)
      } catch (err: any) {
        const detail = err?.data?.detail || err?.message || ''
        showError(
          /deployment_mode=offline|not configured|no cloud|refused/i.test(detail)
            ? 'Cloud server not configured — cloud upload is unavailable.'
            : detail || 'Failed to queue cloud upload'
        )
      } finally {
        setUploadingKey((k) => (k === key ? null : k))
      }
    },
    [confirm, t, showSuccess, showError]
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

  // Opens with 5 minutes from the playhead, every shown camera ticked.
  const toggleExport = () => {
    if (exportMode) {
      setExportMode(false)
      return
    }
    const start = clamp(previewMs ?? masterMs, dayStart, dayEnd - 1)
    const end = Math.min(start + 300_000, dayEnd)
    setExportSel({ startMs: start, endMs: end })
    setExportCamIds(selectedIds)
    setExportDrawn(false)
    setExportMode(true)
    // At the 24h zoom a 5-minute range is a 3-pixel sliver: open on the
    // 1h window around it, so the range can be seen and dragged.
    const span = ZOOMS[2].span
    if (view.end - view.start > span) {
      const from = clamp((start + end) / 2 - span / 2, dayStart, Math.max(dayStart, dayEnd - span))
      setZoomIdx(2)
      setView({ start: from, end: from + span })
    }
  }

  const setExportLength = (ms: number) =>
    setExportSel((s) => s && { startMs: s.startMs, endMs: Math.min(s.startMs + ms, dayEnd) })

  const toggleExportCam = (id: number) =>
    setExportCamIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]))

  const patchJob = (id: string, patch: Partial<ClipJob>) =>
    setClipJobs((jobs) => jobs.map((j) => (j.id === id ? { ...j, ...patch } : j)))

  // One clip: ticket, then the bytes into the picked file (Chromium), into
  // memory for short clips elsewhere, or to the browser's own download.
  const runClipJob = async (
    job: ClipJob,
    cam: OverviewCamera,
    startIso: string,
    open: ((name: string) => Promise<ClipSink>) | 'unsupported',
  ) => {
    const ctrl = new AbortController()
    clipAborts.current.set(job.id, ctrl)
    try {
      const res: any = await apiService.createClipExportTicket({
        camera_id: cam.camera_id,
        start: startIso,
        duration: job.durationSec,
        filename: job.filename,
      })
      const url = res.data?.download_url
      if (!url) throw new Error('No download URL returned')
      if (open === 'unsupported' && job.durationSec > MAX_BUFFERED_CLIP_SECONDS) {
        nativeDownload(url)
        patchJob(job.id, { phase: 'browser' })
        return
      }
      const sink = open === 'unsupported' ? undefined : await open(job.filename)
      const out = await fetchClip(url, {
        sink,
        signal: ctrl.signal,
        onProgress: (p) => patchJob(job.id, { phase: p.phase, bytes: p.bytes, mediaSeconds: p.mediaSeconds }),
      })
      if (out.blob) saveBlob(out.blob, job.filename)
      patchJob(job.id, { phase: 'done', bytes: out.bytes, mediaSeconds: job.durationSec })
      showSuccess(t('playback.dlSavedToast', { name: job.filename }))
    } catch (e: any) {
      if (ctrl.signal.aborted) patchJob(job.id, { phase: 'cancelled' })
      else {
        const msg = e?.data?.detail || e?.message || t('playback.exportFailed')
        patchJob(job.id, { phase: 'failed', error: msg })
        showError(`${cam.camera_name}: ${msg}`)
      }
    } finally {
      clipAborts.current.delete(job.id)
    }
  }

  const runExport = async (cams: OverviewCamera[]) => {
    if (!exportSel || cams.length === 0) return
    const start = new Date(exportSel.startMs)
    const stamp = start.toISOString().replace(/[:.]/g, '-').slice(0, 19)
    const durationSec = Math.max(1, Math.round((exportSel.endMs - exportSel.startMs) / 1000))
    const range = `${clockTime(exportSel.startMs)} → ${clockTime(exportSel.endMs)}`
    const filenames = cams.map((c) => `${(c.camera_name || `camera-${c.camera_id}`).replace(/\s+/g, '-')}_${stamp}.mp4`)
    // Where to save is asked first, while the click still counts as a user
    // gesture (the pickers refuse otherwise). Closing the dialog cancels.
    let open: ((name: string) => Promise<ClipSink>) | 'unsupported' = 'unsupported'
    if (canPickFiles()) {
      const picked = await pickDestination(filenames)
      if (picked === null) return
      open = picked
    }
    const jobs: ClipJob[] = cams.map((c, i) => ({
      id: `${Date.now()}-${c.camera_id}`,
      camera: c.camera_name,
      range,
      filename: filenames[i],
      durationSec,
      phase: 'starting',
      bytes: 0,
      mediaSeconds: 0,
    }))
    setClipJobs((prev) => [...jobs, ...prev])
    setExportMode(false)
    await Promise.all(jobs.map((j, i) => runClipJob(j, cams[i], start.toISOString(), open)))
  }

  const cancelClipJob = (id: string) => clipAborts.current.get(id)?.abort()
  const dismissClipJob = (id: string) => setClipJobs((jobs) => jobs.filter((j) => j.id !== id))
  const clearFinishedClipJobs = () => setClipJobs((jobs) => jobs.filter(clipJobRunning))

  // A download in flight dies with the page: warn before leaving it, and
  // abort cleanly if the page goes anyway.
  const clipsRunning = clipJobs.some(clipJobRunning)
  useEffect(() => {
    if (!clipsRunning) return
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [clipsRunning])
  useEffect(() => {
    const aborts = clipAborts.current
    return () => aborts.forEach((c) => c.abort())
  }, [])

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
  const exportCams = selectedCams.filter((c) => exportCamIds.includes(c.camera_id))
  const exportLen = exportSel ? exportSel.endMs - exportSel.startMs : 0
  const exportProblem =
    exportLen < 1000
      ? t('playback.exportRangeHint')
      : exportLen > MAX_EXPORT_MS
        ? t('playback.exportTooLong')
        : exportCams.length === 0
          ? t('playback.exportNoCamera')
          : null
  const clockTime = (ms: number) =>
    fmt.time(ms, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })

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
          <p className="text-[var(--text-dim)]">
            {overviewError && (overviewError as any)?.message === 'API_NETWORK_ERROR'
              ? t('dashboard.failedRecordings')
              : overviewError || t('playback.noRecordings')}
          </p>
          <p className="text-sm text-[var(--text-dim)] mt-1">
            {overviewError ? t('playback.tryRefreshing') : t('playback.recordingsAppear')}
          </p>
          <button onClick={() => loadOverview()} className="btn-primary btn mt-4">
            {t('playback.refresh')}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-2 lg:flex-row lg:h-[calc(100vh-5rem)]">
      {/* Stage: one panel in the dashboard widgets' grammar — slim header,
          the wall on black split by hairlines, transport and timeline as
          its footer. */}
      <div
        ref={stageRef}
        className="flex-1 min-w-0 flex flex-col min-h-0 border border-[var(--border)] bg-[var(--panel-2)] overflow-hidden"
      >
        <div className="h-8 shrink-0 flex items-center gap-2 px-2.5 border-b border-[var(--border)] bg-[color-mix(in_oklab,var(--bg-2)_55%,var(--panel-2))]">
          <MonitorPlay size={13} className="text-[var(--text-dim)] shrink-0" aria-hidden="true" />
          <h1 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text)] whitespace-nowrap">{t('playback.pageTitle')}</h1>
          <span className="text-[11px] text-[var(--text-dim)] font-mono tabular-nums truncate min-w-0">
            {selectedDate && `${formatDateLong(selectedDate, fmt)} · `}
            {selectedCams.length} {t('playback.cameraCount')}
            {selectedDate && dayTotal.cams > 0 && (
              <span title={`Recorded on this day across ${dayTotal.cams} camera${dayTotal.cams !== 1 ? 's' : ''}`}>
                {` · ${formatDuration(dayTotal.seconds)} ${t('playback.recorded')}`}
              </span>
            )}
          </span>
          {!mediamtxAvailable && (
            <span className="text-[11px] text-[var(--warn)] truncate">Playback server offline — playback unavailable</span>
          )}
          {deepLinkMiss && (
            <span className="text-[11px] text-[var(--warn)] truncate" role="status">
              {deepLinkMiss.nearestMs != null
                ? t('playback.noFootageAtNearest', {
                    at: fmt.time(deepLinkMiss.askedMs, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }),
                    next: fmt.time(deepLinkMiss.nearestMs, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }),
                  })
                : t('playback.noFootageAt', {
                    at: fmt.time(deepLinkMiss.askedMs, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }),
                  })}
            </span>
          )}
          {cloudWatch && cloudStatus && (
            <span className="flex items-center gap-1 text-[11px] text-[var(--text-dim)] font-mono tabular-nums truncate" title={t('playback.cloudStatus')}>
              <Upload size={11} className="text-[var(--ok)] animate-pulse shrink-0" />
              {t('playback.cloudProgress', {
                queued: cloudStatus.queue_size,
                done: cloudStatus.stats?.completed_total ?? 0,
                failed: cloudStatus.stats?.failed_total ?? 0,
              })}
            </span>
          )}
          {!isFullscreen && (
            <button
              onClick={togglePanel}
              title={panelOpen ? 'Hide calendar & camera panel' : 'Show calendar & camera panel'}
              aria-label={panelOpen ? 'Hide calendar & camera panel' : 'Show calendar & camera panel'}
              className="ml-auto p-0.5 text-[var(--text-dim)] hover:text-[var(--text)]"
            >
              {panelOpen ? <PanelRightClose size={14} /> : <PanelRightOpen size={14} />}
            </button>
          )}
        </div>

        {/* Video grid */}
        <div
          className="flex-1 min-h-0 grid gap-px bg-[#1a1f29]"
          style={{
            gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
            gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))`,
          }}
        >
          {overviewLoading ? (
            <div className="bg-black animate-pulse flex flex-col items-center justify-center gap-2 py-16 text-white/50">
              <Loader2 size={22} className="animate-spin text-[var(--accent)]" />
              <span className="text-sm">{t('playback.loadingRecordings')}</span>
            </div>
          ) : selectedCams.length === 0 ? (
            <div className="bg-black flex flex-col items-center justify-center gap-2 py-16 text-white/45">
              <Video size={20} />
              <span className="text-[10px] font-mono uppercase tracking-[0.2em]">{t('playback.selectCameras')}</span>
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
                {...(cameraAspects[cam.camera_id] ?? {})}
              />
            ))
          )}
        </div>

        {/* Transport controls */}
        <div className="shrink-0 flex items-center gap-0.5 px-1.5 py-0.5 border-t border-[var(--border)] flex-wrap">
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

          <button
            onClick={toggleExport}
            disabled={selectedCams.length === 0}
            title={exportMode ? t('common.cancel') : t('playback.exportTitle')}
            className={`ml-1 px-1.5 py-0.5 flex items-center gap-1 text-[10px] font-medium border transition-colors disabled:opacity-40 ${
              exportMode
                ? 'bg-[var(--accent)] border-[var(--accent)] text-white'
                : 'border-[var(--border)] text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel)]'
            }`}
          >
            <Scissors size={12} />
            {t('playback.export')}
          </button>

          <IconBtn title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'} onClick={toggleFullscreen}>
            {isFullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </IconBtn>
        </div>

        {/* Clip export: the range (drawn on the timeline), its length, which
            cameras, and Download. */}
        {exportMode && exportSel && (
          <div className="shrink-0 flex flex-wrap items-center gap-x-2 gap-y-1 px-2 py-1 border-t border-[var(--border)] text-[11px]">
            <Scissors size={12} className="text-[var(--warn)] shrink-0" />
            <span className="font-mono tabular-nums">
              {clockTime(exportSel.startMs)} → {clockTime(exportSel.endMs)}
            </span>
            <span className="text-[var(--text-dim)] font-mono">{formatClipLength(exportLen)}</span>
            <div className="flex items-center overflow-hidden border border-[var(--border)]" title={t('playback.exportLength')}>
              {EXPORT_PRESETS.map((p) => (
                <button
                  key={p.label}
                  onClick={() => setExportLength(p.ms)}
                  className={`px-1.5 py-0.5 text-[10px] font-mono transition-colors ${
                    Math.abs(exportLen - p.ms) < 1000
                      ? 'bg-[var(--accent)] text-white'
                      : 'text-[var(--text-dim)] hover:bg-[var(--panel)]'
                  }`}
                >
                  {p.label}
                </button>
              ))}
            </div>
            {selectedCams.length > 1 &&
              selectedCams.map((c) => {
                const on = exportCamIds.includes(c.camera_id)
                return (
                  <button
                    key={c.camera_id}
                    onClick={() => toggleExportCam(c.camera_id)}
                    className={`flex items-center gap-1 px-1.5 py-0.5 border text-[10px] max-w-36 ${
                      on ? 'border-[var(--accent)] text-[var(--text)]' : 'border-[var(--border)] text-[var(--text-dim)]'
                    }`}
                  >
                    <span
                      className={`w-3 h-3 shrink-0 border flex items-center justify-center ${
                        on ? 'bg-[var(--accent)] border-[var(--accent)]' : 'border-[var(--border)]'
                      }`}
                    >
                      {on && <Check size={9} className="text-white" />}
                    </span>
                    <span className="truncate">{c.camera_name}</span>
                  </button>
                )
              })}
            {/* The one instruction this mode needs, made hard to miss: until a
                range is dragged it says how; then it confirms. A real problem
                (too long, no camera) takes its place in warning colours. */}
            {exportProblem && (exportLen >= 1000 || exportDrawn) ? (
              <span className="flex items-center gap-1.5 px-2 py-0.5 border border-[var(--warn)] bg-[var(--warn)]/15 text-[var(--warn)] font-medium">
                {exportProblem}
              </span>
            ) : exportDrawn ? (
              <span className="flex items-center gap-1.5 px-2 py-0.5 border border-[var(--ok)]/50 bg-[var(--ok)]/10 text-[var(--text)]">
                <Check size={12} className="text-[var(--ok)] shrink-0" />
                {t('playback.exportRangeSet')}
              </span>
            ) : (
              <span className="flex items-center gap-1.5 px-2 py-0.5 border border-[var(--warn)]/60 bg-[var(--warn)]/15 text-[var(--text)] animate-[pulse_2s_ease-in-out_3]">
                <MousePointerClick size={13} className="text-[var(--warn)] shrink-0" />
                <span>
                  <strong>{t('playback.exportDragStrong')}</strong> {t('playback.exportDragRest')}
                </span>
              </span>
            )}
            <span className="text-[10px] text-[var(--text-dim)] hidden xl:inline">{t('playback.exportLimits')}</span>
            <div className="flex-1" />
            <button onClick={() => setExportMode(false)} className="btn px-2 py-0.5 text-[11px]">
              {t('common.cancel')}
            </button>
            <button
              onClick={() => runExport(exportCams)}
              disabled={!!exportProblem}
              className="btn-primary btn px-2 py-0.5 text-[11px] flex items-center gap-1 disabled:opacity-50"
            >
              <Download size={12} />
              {exportCams.length > 1
                ? t('playback.exportDownloadN', { n: exportCams.length })
                : t('playback.exportDownload')}
            </button>
          </div>
        )}

        {/* Multi-camera timeline */}
        {timelineRows.length > 0 && view.end > view.start && (
          <div className="shrink-0 px-2 pt-0.5 pb-1 border-t border-[var(--border)]">
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
                selecting={exportMode}
                selection={exportSel}
                onSelectionChange={(s) => {
                  setExportSel(s)
                  setExportDrawn(true)
                }}              />
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
            className="w-full h-8 flex items-center gap-2 px-2.5 bg-[color-mix(in_oklab,var(--bg-2)_55%,var(--panel-2))] hover:bg-[var(--panel)]"
            aria-expanded={calOpen}
          >
            <CalendarDays size={13} className="text-[var(--text-dim)]" />
            <span className="text-[11px] font-semibold uppercase tracking-[0.12em] truncate">{selectedDate ? formatDateLong(selectedDate, fmt) : t('playback.selectDate')}</span>
            <ChevronDown size={13} className={`ml-auto text-[var(--text-dim)] transition-transform ${calOpen ? '' : '-rotate-90'}`} />
          </button>
          {calOpen && (
            <div className="px-2.5 pb-2.5 border-t border-[var(--border)] pt-1.5">
              <RecordingCalendar selected={selectedDate} onSelect={selectDate} markedDates={markedDates} />
            </div>
          )}
        </div>

        {/* Camera checklist */}
        <div className="bg-[var(--panel-2)] border border-[var(--border)] flex flex-col min-h-0 lg:flex-1">
          <div className="shrink-0 h-8 flex items-center gap-2 px-2.5 border-b border-[var(--border)] bg-[color-mix(in_oklab,var(--bg-2)_55%,var(--panel-2))]">
            <Video size={13} className="text-[var(--text-dim)]" />
            <span className="text-[11px] font-semibold uppercase tracking-[0.12em]">{t('playback.cameras')}</span>
            <span className="ml-auto text-[11px] font-mono tabular-nums text-[var(--text-dim)]">
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
            onUpload={user?.is_superuser ? uploadDay : undefined}
            uploadingKey={uploadingKey}
          />
        </div>
      </aside>
      )}

      <ClipDownloadPanel
        jobs={clipJobs}
        onCancel={cancelClipJob}
        onDismiss={dismissClipJob}
        onClearFinished={clearFinishedClipJobs}
      />
    </div>
  )
}

function PageTitle() {
  const { t } = useTranslation()
  return (
    <h1 className="text-lg font-semibold flex items-center gap-2">
      <MonitorPlay size={20} className="text-[var(--accent)]" />
      {t('playback.pageTitle')}
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
  onUpload,
  uploadingKey,
}: {
  overview: OverviewCamera[] | null
  overviewLoading: boolean
  recByCamDate: Map<number, Map<string, DailyRecording>>
  selectedDate: string | null
  selectedIds: number[]
  activeId: number | null
  onToggle: (id: number) => void
  /** Superusers only: queue this camera's selected day for cloud upload. */
  onUpload?: (cam: OverviewCamera, date: string) => void
  uploadingKey: string | null
}) {
  const { t } = useTranslation()
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
        const uploading = uploadingKey === `${cam.camera_id}:${selectedDate}`
        return (
          <div
            key={cam.camera_id}
            className={`group flex items-center border-l-2 transition-colors ${
              cam.camera_id === activeId ? 'border-l-[var(--accent)]' : 'border-l-transparent'
            } ${checked ? 'bg-[var(--accent)]/10' : ''} ${disabled ? '' : 'hover:bg-[var(--panel)]'}`}
          >
          <button
            onClick={() => !disabled && onToggle(cam.camera_id)}
            disabled={disabled}
            title={rec ? `${formatDuration(rec.total_duration)} recorded` : 'No recordings on this date'}
            className={`flex-1 min-w-0 flex items-center gap-2 pl-2.5 pr-1 py-1.5 text-xs text-left focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent)] ${
              cam.camera_id === activeId ? 'text-[var(--accent)]' : ''
            } ${disabled ? 'opacity-40 cursor-not-allowed' : ''}`}
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
          {onUpload && rec && selectedDate && (
            <button
              onClick={() => onUpload(cam, selectedDate)}
              disabled={uploading}
              title={`${t('playback.upload')}: ${t('playback.confirmUploadTitle')}`}
              aria-label={`${t('playback.upload')} ${cam.camera_name} ${selectedDate}`}
              className="shrink-0 p-1.5 mr-1 text-[var(--text-dim)] hover:text-[var(--ok)] disabled:opacity-60"
            >
              {uploading ? <Loader2 size={12} className="animate-spin" /> : <Upload size={12} />}
            </button>
          )}
          </div>
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
