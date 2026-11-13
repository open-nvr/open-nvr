// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { CameraOff, ChevronLeft, ChevronRight, ExternalLink, Maximize2, Minimize2, Pause, Play, Plus, Tv } from 'lucide-react'
import { clsx } from 'clsx'
import { useTranslation } from '../../../i18n'
import { useCameras, type CameraItem } from '../../../lib/queries'
import { apiService } from '../../../lib/apiService'
import { rebaseToCurrentOrigin } from '../../../lib/streamUrl'
import { useCameraStatus } from '../../../hooks/useCameraStatus'
import { usePageVisible } from '../../../hooks/usePageVisible'
import { VideoPlayer } from '../../../components/VideoPlayer'
import { detectionsPreferred } from '../../../lib/detectionFeed'
import { WidgetFrame, MiniSegment, cameraState, shortAge, STATE_INK, type CamState } from '../WidgetFrame'
import { webcamOfflineLabel } from '../../../lib/hostAgent'

const TOUR_MS = 15_000
const SLOT_OPTIONS = [1, 4, 6, 9] as const
type Slots = (typeof SLOT_OPTIONS)[number]

const PREFS_KEY = 'opennvr.dashboard.wall'
type WallPrefs = { slots: Slots; tour: boolean }

function loadPrefs(): WallPrefs {
  try {
    const p = JSON.parse(localStorage.getItem(PREFS_KEY) || 'null')
    if (p && SLOT_OPTIONS.includes(p.slots) && typeof p.tour === 'boolean') return p
  } catch { /* storage unavailable */ }
  return { slots: 4, tour: true }
}

/** Columns × rows for n tiles — never more cells than there are cameras. */
function gridFor(n: number): [number, number] {
  if (n <= 1) return [1, 1]
  if (n === 2) return [2, 1]
  if (n <= 4) return [2, 2]
  if (n <= 6) return [3, 2]
  return [3, 3]
}

const STATE_RANK: Record<CamState, number> = { online: 0, degraded: 1, error: 2, offline: 3 }

type StreamInfo = { urls?: { webrtc?: string; webrtc_sub?: string; hls?: string }; token?: string }

/**
 * One live tile. Prefers the low-res substream when the server publishes
 * one (a glance wall does not need main-stream resolution, and several
 * main streams decoding at once is what makes a dashboard heavy), and
 * falls back to the main stream if the substream path does not exist.
 */
function LiveTile({ cam, spotlit, onSpotlight, playing }: {
  cam: CameraItem
  spotlit: boolean
  onSpotlight: () => void
  playing: boolean
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const state = cameraState(cam)
  const { version } = useCameraStatus(cam.id)
  const [preferSub, setPreferSub] = useState(true)

  // The stream token lives 60 minutes; cache the info a little under that
  // so a remount (page flip, spotlight) reuses it instead of re-minting.
  const info = useQuery({
    queryKey: ['stream-info', cam.id, version],
    enabled: playing && state !== 'offline' && state !== 'error',
    queryFn: async () => {
      const { data } = await apiService.getStreamUrls(cam.id)
      return data as StreamInfo
    },
    staleTime: 45 * 60_000,
    gcTime: 50 * 60_000,
  })

  const lastRefresh = useRef(0)
  const onAuthExpired = useCallback(() => {
    const now = Date.now()
    if (now - lastRefresh.current < 10_000) return
    lastRefresh.current = now
    qc.invalidateQueries({ queryKey: ['stream-info', cam.id] })
  }, [qc, cam.id])

  const sub = info.data?.urls?.webrtc_sub
  const usingSub = preferSub && !!sub
  const whep = rebaseToCurrentOrigin(usingSub ? sub : info.data?.urls?.webrtc)
  const hls = rebaseToCurrentOrigin(info.data?.urls?.hls)
  const onError = useCallback((msg: string) => {
    // A 404 on the substream while the camera itself is up means this
    // camera has no substream path: use the main stream instead.
    if (usingSub && msg === 'Camera offline' && cam.live_online) setPreferSub(false)
  }, [usingSub, cam.live_online])

  const live = state === 'online' || state === 'degraded'
  const rec = cam.recording_state

  return (
    <div className="group relative bg-black overflow-hidden min-w-0 min-h-0">
      {live && playing && (whep || hls) ? (
        <div className="absolute inset-0">
        <VideoPlayer
          key={`${cam.id}-${version}-${usingSub ? 's' : 'm'}`}
          mode="live"
          chrome="none"
          whepUrl={whep}
          hlsUrl={hls}
          mediamtxToken={info.data?.token}
          onAuthExpired={onAuthExpired}
          onError={onError}
          preferredStreamType="webrtc"
          autoPlay
          muted
          displayAspectOverride={cam.display_aspect_ratio}
          offlineLabel={webcamOfflineLabel(cam as any)}
          cameraId={cam.id}
          className="w-full h-full !bg-black"
          showDetections={detectionsPreferred()}
        />
        </div>
      ) : (
        <div
          className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 text-white/45"
          style={{ background: 'repeating-linear-gradient(135deg, #07090d 0 10px, #0b0f16 10px 20px)' }}
        >
          {live ? (
            <div className="w-5 h-5 border-2 border-white/20 border-t-white/70 rounded-full animate-spin" />
          ) : (
            <>
              <CameraOff size={18} />
              <div className="text-[10px] font-mono uppercase tracking-[0.2em]">
                {state === 'error' ? t('dashboard.statusError') : t('dashboard.statusOffline')}
              </div>
              {cam.last_recording_at && (
                <div className="text-[10px] text-white/35">
                  {t('dashboard.lastFootage')} {shortAge(cam.last_recording_at)}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* Label band: legible over any picture without a solid bar. */}
      <div className="pointer-events-none absolute inset-x-0 top-0 h-9 bg-gradient-to-b from-black/75 to-transparent" />
      <div className="pointer-events-none absolute left-2 right-2 top-1.5 flex items-center gap-1.5 text-white">
        <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: STATE_INK[state] }} aria-hidden="true" />
        <span className="text-[11px] font-medium truncate [text-shadow:0_1px_2px_rgba(0,0,0,0.9)]">{cam.name || `Camera ${cam.id}`}</span>
        <span className="ml-auto shrink-0 flex items-center leading-none">
          {rec === 'recording' ? (
            <span className="flex items-center gap-1 text-[9px] font-bold tracking-[0.15em] text-[var(--on-video-danger)] [text-shadow:0_1px_2px_rgba(0,0,0,0.9)]">
              <span className="w-1.5 h-1.5 rounded-full bg-[var(--on-video-danger)] animate-pulse" aria-hidden="true" />REC
            </span>
          ) : rec === 'stalled' ? (
            <span className="text-[9px] font-bold tracking-[0.15em] text-[var(--on-video-warn)] [text-shadow:0_1px_2px_rgba(0,0,0,0.9)]">{t('dashboard.stalled').toUpperCase()}</span>
          ) : rec === 'degraded' ? (
            <span className="text-[9px] font-bold tracking-[0.15em] text-[var(--on-video-danger)] [text-shadow:0_1px_2px_rgba(0,0,0,0.9)]">{t('dashboard.statusDegraded').toUpperCase()}</span>
          ) : null}
        </span>
      </div>

      {/* Hover actions */}
      <div className="absolute right-1.5 bottom-1.5 flex gap-1 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
        <button
          type="button"
          onClick={onSpotlight}
          className="p-1.5 bg-black/60 hover:bg-black/80 text-white/90 border border-white/10"
          title={spotlit ? t('dashboard.backToWall') : t('dashboard.spotlight')}
          aria-label={spotlit ? t('dashboard.backToWall') : t('dashboard.spotlight')}
        >
          {spotlit ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
        </button>
        <button
          type="button"
          onClick={() => navigate(`/live?camera=${cam.id}`)}
          className="p-1.5 bg-black/60 hover:bg-black/80 text-white/90 border border-white/10"
          title={t('dashboard.openLiveView')}
          aria-label={t('dashboard.openLiveView')}
        >
          <ExternalLink size={12} />
        </button>
      </div>
      {/* Double-click anywhere on the picture to spotlight / return. */}
      <div className="absolute inset-0 top-8 bottom-9" onDoubleClick={onSpotlight} aria-hidden="true" />
    </div>
  )
}

/**
 * A glance at the cameras: a small wall of live feeds that tours through the
 * fleet page by page. Healthy cameras come first, so the first page is the
 * one worth watching; offline cameras show as a still placeholder and hold
 * no stream. Hovering pauses the tour; any tile can be spotlit to fill the
 * widget.
 */
export function LiveWallWidget({ editing, onRemove }: { editing?: boolean; onRemove?: () => void }) {
  const { t } = useTranslation()
  const cams = useCameras()
  const visible = usePageVisible()
  const [prefs, setPrefs] = useState<WallPrefs>(loadPrefs)
  const [page, setPage] = useState(0)
  const [hover, setHover] = useState(false)
  const [spotlight, setSpotlight] = useState<number | null>(null)

  useEffect(() => {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)) } catch { /* ignore */ }
  }, [prefs])

  const ordered = useMemo(() => {
    const list = [...(cams.data?.cameras ?? [])]
    list.sort((a, b) => STATE_RANK[cameraState(a)] - STATE_RANK[cameraState(b)] || a.id - b.id)
    return list
  }, [cams.data])

  const pages = Math.max(1, Math.ceil(ordered.length / prefs.slots))
  const current = Math.min(page, pages - 1)
  const shown = ordered.slice(current * prefs.slots, current * prefs.slots + prefs.slots)
  const spotCam = spotlight != null ? ordered.find((c) => c.id === spotlight) : undefined
  const tiles = spotCam ? [spotCam] : shown
  const [cols, rows] = gridFor(tiles.length)

  const touring = prefs.tour && pages > 1 && !spotCam && !editing
  const paused = hover || !visible
  useEffect(() => {
    if (!touring || paused) return
    const id = window.setTimeout(() => setPage((p) => (p + 1) % pages), TOUR_MS)
    return () => window.clearTimeout(id)
  }, [touring, paused, current, pages])

  const step = (d: number) => setPage((p) => (Math.min(p, pages - 1) + d + pages) % pages)

  return (
    <WidgetFrame
      icon={<Tv size={13} />}
      title={t('dashboard.w.live')}
      editing={editing}
      onRemove={onRemove}
      meta={ordered.length ? (
        <span className="font-mono tabular-nums">
          {spotCam ? spotCam.name : `${ordered.length} ${t('dashboard.camerasPlural')}${pages > 1 ? ` · ${current + 1}/${pages}` : ''}`}
        </span>
      ) : undefined}
      actions={ordered.length ? (
        <>
          {pages > 1 && !spotCam && (
            <>
              <button type="button" onClick={() => step(-1)} className="p-0.5 text-[var(--text-dim)] hover:text-[var(--text)]" aria-label={t('dashboard.prevPage')} title={t('dashboard.prevPage')}>
                <ChevronLeft size={14} />
              </button>
              <button type="button" onClick={() => step(1)} className="p-0.5 text-[var(--text-dim)] hover:text-[var(--text)]" aria-label={t('dashboard.nextPage')} title={t('dashboard.nextPage')}>
                <ChevronRight size={14} />
              </button>
              <button
                type="button"
                onClick={() => setPrefs((p) => ({ ...p, tour: !p.tour }))}
                className={clsx('p-0.5 mr-1', prefs.tour ? 'text-[var(--accent)]' : 'text-[var(--text-dim)] hover:text-[var(--text)]')}
                aria-pressed={prefs.tour}
                title={prefs.tour ? t('dashboard.tourOn') : t('dashboard.tourOff')}
                aria-label={t('dashboard.tour')}
              >
                {prefs.tour ? <Pause size={13} /> : <Play size={13} />}
              </button>
            </>
          )}
          <MiniSegment
            label={t('dashboard.tilesPerPage')}
            value={prefs.slots}
            options={SLOT_OPTIONS.map((n) => ({ value: n, label: String(n), title: `${n} ${t('dashboard.tilesPerPage').toLowerCase()}` }))}
            onChange={(n) => { setPrefs((p) => ({ ...p, slots: n })); setPage(0); setSpotlight(null) }}
          />
        </>
      ) : undefined}
      bodyClassName="bg-black"
    >
      <div className="absolute inset-0" onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
        {touring && (
          <div className="absolute top-0 inset-x-0 h-[2px] z-10 bg-white/5">
            <div
              key={`${current}-${prefs.slots}`}
              className="h-full bg-[var(--accent)] dash-tour-progress"
              style={{ animationDuration: `${TOUR_MS}ms`, animationPlayState: paused ? 'paused' : 'running' }}
            />
          </div>
        )}
        {cams.isPending ? (
          <div className="absolute inset-0 grid grid-cols-2 grid-rows-2 gap-px">
            {Array.from({ length: 4 }).map((_, i) => <div key={i} className="bg-[#0b0f16] animate-pulse" />)}
          </div>
        ) : ordered.length === 0 ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-white/60 text-sm">
            <CameraOff size={22} />
            <div>{t('dashboard.noCameras')}</div>
            <Link to="/cameras" className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-white/15 hover:bg-white/5">
              <Plus size={12} /> {t('dashboard.addCamera')}
            </Link>
          </div>
        ) : (
          <div
            className="absolute inset-0 grid gap-px bg-[#1a1f29]"
            style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`, gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))` }}
          >
            {tiles.map((c) => (
              <LiveTile
                key={c.id}
                cam={c}
                playing={visible}
                spotlit={spotCam?.id === c.id}
                onSpotlight={() => setSpotlight((s) => (s === c.id ? null : c.id))}
              />
            ))}
          </div>
        )}
      </div>
    </WidgetFrame>
  )
}
