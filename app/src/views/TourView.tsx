// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * A Live View tour running on a display board: the wall shows one page of a
 * tour's cameras, then the next, for ever (routers/tours.py, lib/tourPlan.ts).
 *
 * Kept apart from LiveView on purpose. Live View's grid is an operator's own
 * arrangement, saved in their browser; a tour never reads or writes it.
 *
 * Built for a screen nobody touches:
 * * moving the mouse does not pause it — pausing is a button (or Space), and
 *   a paused wall says so until it is resumed;
 * * tiles play the low-res substream, falling back to the main stream for a
 *   camera whose substream does not work, and remembering that for ten
 *   minutes so later pages do not wait on it again;
 * * the next page's stream tokens are fetched a few seconds before it shows;
 * * edits to the tour reach a running wall within five minutes, and a failed
 *   fetch keeps showing the tour it has while it retries.
 *
 * Someone who may edit the tour can open the page on screen in the tour editor
 * (views/TourEditor.tsx); the wall then carries on from that page.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, BellRing, CameraOff, ChevronLeft, ChevronRight, ExternalLink, Maximize, Minimize, Pause, Pencil, Play, PauseCircle, Route, X } from 'lucide-react'
import { clsx } from 'clsx'
import { useDateFormat, useTranslation } from '../i18n'
import { apiService } from '../lib/apiService'
import { rebaseToCurrentOrigin } from '../lib/streamUrl'
import { useCameras, type CameraItem } from '../lib/queries'
import { resolveLayout, type LayoutDefinition, type WindowLayoutSettings } from '../lib/liveLayouts'
import { autoGrid, buildPages, camerasOf, formatDuration, wrapIndex, type TourPage } from '../lib/tourPlan'
import type { ActiveAlert } from '../lib/tourAlerts'
import { DEFAULT_TOUR_SETTINGS, type Tour } from '../services/tourService'
import { useCameraStatus } from '../hooks/useCameraStatus'
import { useFullscreen } from '../hooks/useFullscreen'
import { usePageVisible } from '../hooks/usePageVisible'
import { usePermissions } from '../hooks/usePermissions'
import { useTourAlerts } from '../hooks/useTourAlerts'
import { VideoPlayer } from '../components/VideoPlayer'
import { detectionsPreferred } from '../lib/detectionFeed'
import { cameraState, STATE_INK } from './dashboard/WidgetFrame'

/** The bar hides after this long without the mouse moving. */
const BAR_HIDE_MS = 3_000
/** Fetch the next page's stream tokens this long before it shows; with
 *  "start the next page early", start its players hidden then too. */
const PREFETCH_LEAD_MS = 5_000
/** The crossfade's length; .tour-fade-out in index.css runs the same. */
const FADE_MS = 600
/** A substream that has not played after this long is treated as broken. */
const SUB_STALL_MS = 12_000
/** The stream token lives 60 minutes; reuse the info a little under that. */
const STREAM_STALE_MS = 45 * 60_000

/** How long a camera whose substream failed stays on the main stream before
 *  the substream is tried again: a slow start once must not cost a 20-tile
 *  wall its cheap streams for the rest of the day. */
const SUB_RETRY_MS = 10 * 60_000

/** Each camera's last frame on this wall (a small JPEG data URL), shown in
 *  its tile until the video plays, so a tile coming back is never black. */
const LAST_FRAME = new Map<number, string>()
const LAST_FRAME_MAX = 400
const LAST_FRAME_WIDTH = 320
/** How often a playing tile refreshes its last frame. */
const LAST_FRAME_EVERY_MS = 20_000
function keepFrame(cameraId: number, video: HTMLVideoElement) {
  if (!video.videoWidth || !video.videoHeight) return
  try {
    const canvas = document.createElement('canvas')
    canvas.width = LAST_FRAME_WIDTH
    canvas.height = Math.round((LAST_FRAME_WIDTH * video.videoHeight) / video.videoWidth)
    canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height)
    LAST_FRAME.delete(cameraId)
    LAST_FRAME.set(cameraId, canvas.toDataURL('image/jpeg', 0.6))
    // Oldest first out: a 1000-camera tour keeps the most recent few hundred.
    while (LAST_FRAME.size > LAST_FRAME_MAX) LAST_FRAME.delete(LAST_FRAME.keys().next().value!)
  } catch {
    // A frame the browser will not hand over (a tainted canvas): no still, no harm.
  }
}

/** Cameras whose substream failed, and until when they start on the main stream. */
const NO_SUB = new Map<number, number>()
const subBlocked = (cameraId: number) => {
  const until = NO_SUB.get(cameraId)
  if (until == null) return false
  if (Date.now() < until) return true
  NO_SUB.delete(cameraId)
  return false
}

type StreamInfo = { urls?: { webrtc?: string; webrtc_sub?: string; hls?: string }; token?: string }

const streamKey = (cameraId: number) => ['tour-stream', cameraId] as const
const fetchStream = async (cameraId: number) => {
  const { data } = await apiService.getTourStreamInfo(cameraId)
  return data as StreamInfo
}

/**
 * `display` is the bare display route (/display/tour/:id): the whole browser
 * window, no app shell, for a board that runs in kiosk mode. Without it the
 * tour sits inside the app like any other page (/live/tour/:id).
 */
export function TourView({ display = false }: { display?: boolean }) {
  const { t } = useTranslation()
  const fmt = useDateFormat()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { tourId } = useParams()
  const id = Number(tourId)
  const validId = Number.isInteger(id) && id > 0
  const visible = usePageVisible()
  const wallRef = useRef<HTMLDivElement>(null)
  const { toggle: toggleFs, isFullscreen } = useFullscreen(wallRef as RefObject<HTMLDivElement>)

  const tour = useQuery({
    queryKey: ['tour', id],
    enabled: validId,
    queryFn: async () => (await apiService.getTour(id)).data as Tour,
    // Edits reach a running wall without a reload; a failure retries sooner.
    refetchInterval: (q) => (q.state.status === 'error' ? 30_000 : 5 * 60_000),
    refetchIntervalInBackground: true,
    retry: (count, err: any) => err?.status !== 404 && count < 2,
  })
  const windowSettings = useQuery({
    queryKey: ['window-settings'],
    queryFn: async () => (await apiService.getWindowSettings()).data as WindowLayoutSettings,
    staleTime: 5 * 60_000,
    refetchInterval: 5 * 60_000,
  })
  // Paused cameras too: a tour shows them as paused rather than dropping them.
  const cams = useCameras({ active_only: false })
  const camById = useMemo(() => new Map((cams.data?.cameras ?? []).map((c) => [c.id, c])), [cams.data])

  const layoutsReady = !windowSettings.isPending
  const pages = useMemo(
    () => (tour.data && layoutsReady ? buildPages(tour.data.pages, (lid) => resolveLayout(lid, windowSettings.data)) : []),
    [tour.data, layoutsReady, windowSettings.data],
  )

  const settings = tour.data?.settings ?? DEFAULT_TOUR_SETTINGS
  const warmStart = settings.preload !== false
  const fade = settings.transition !== 'cut'
  const tourCameras = useMemo(() => new Set((tour.data?.pages ?? []).flatMap((s) => camerasOf(s.camera_ids))), [tour.data])
  // Alarm takeover: an alert on a camera brings it to the front; the tour
  // waits underneath and carries on where it was (lib/tourAlerts.ts).
  const { active: alerts, dismiss } = useTourAlerts(tour.data?.enabled ? settings.alerts : undefined, tourCameras)
  const alerting = alerts.length > 0

  const [index, setIndex] = useState(0)
  const [paused, setPaused] = useState(false)
  const [spot, setSpot] = useState<number | null>(null)
  // The page started early, hidden under the one showing (its page key).
  const [preloadKey, setPreloadKey] = useState<string | null>(null)
  const current = pages.length ? Math.min(index, pages.length - 1) : 0
  const page: TourPage | undefined = pages[current]
  const pageKey = page?.key

  const go = useCallback((delta: number) => {
    setSpot(null)
    setIndex((i) => wrapIndex(Math.min(i, Math.max(0, pages.length - 1)), delta, pages.length))
  }, [pages])

  // Back from editing a page (?page=, its place in the tour): carry on from it.
  const [params, setParams] = useSearchParams()
  const startAt = params.get('page')
  useEffect(() => {
    if (startAt == null || !pages.length) return
    const at = pages.findIndex((p) => p.index === Number(startAt))
    if (at >= 0) setIndex(at)
    setParams({}, { replace: true })
  }, [startAt, pages, setParams])

  // A spotlit camera that is no longer on the page (the tour was edited) is let go.
  useEffect(() => {
    if (spot != null && page && !page.cameraIds.includes(spot)) setSpot(null)
  }, [spot, page])

  // The page timer. Time already spent on a page survives pausing, a hidden
  // tab and a refetch of the unchanged tour; only a new page starts from zero.
  const elapsed = useRef(0)
  useEffect(() => { elapsed.current = 0 }, [pageKey])
  const running = !!page && pages.length > 1 && !paused && visible && spot == null && !alerting
  // When the page turns, for the countdown chip: a deadline while running,
  // the time left while held.
  const [deadline, setDeadline] = useState<number | null>(null)
  const heldLeftMs = page ? Math.max(0, page.durationMs - elapsed.current) : 0
  useEffect(() => {
    if (!running || !page) return
    const started = Date.now()
    const remaining = Math.max(0, page.durationMs - elapsed.current)
    setDeadline(started + remaining)
    const next = pages[wrapIndex(current, 1, pages.length)]
    const prefetch = window.setTimeout(() => {
      // Early start: the next page's players mount hidden under this one and
      // fetch their own tokens, so the page is playing when it shows.
      if (warmStart) { setPreloadKey(next.key); return }
      for (const cid of camerasOf(next.cameraIds)) {
        qc.prefetchQuery({ queryKey: streamKey(cid), queryFn: () => fetchStream(cid), staleTime: STREAM_STALE_MS })
      }
    }, Math.max(0, remaining - PREFETCH_LEAD_MS))
    const advance = window.setTimeout(() => go(1), remaining)
    return () => {
      window.clearTimeout(prefetch)
      window.clearTimeout(advance)
      elapsed.current += Date.now() - started
      setDeadline(null)
      // Held (paused, spotlit, an alarm) the hidden page stops: it would
      // otherwise keep a second page of streams open for as long as it lasts.
      setPreloadKey(null)
    }
  }, [running, page, pages, current, go, qc, warmStart])

  // The crossfade: the page that just left stays a moment, fading out over
  // the new one. Worked out while rendering, not in an effect, so the
  // leaving page is never unmounted and remounted (which would flash black).
  const [shownPage, setShownPage] = useState<TourPage | undefined>(page)
  const [leaving, setLeaving] = useState<TourPage | null>(null)
  if (page?.key !== shownPage?.key) {
    setShownPage(page)
    setLeaving(fade && shownPage && !alerting && spot == null ? shownPage : null)
  }
  useEffect(() => {
    if (!leaving) return
    const id = window.setTimeout(() => setLeaving(null), FADE_MS)
    return () => window.clearTimeout(id)
  }, [leaving])

  // The bar shows on mouse movement and hides again; it stays while paused.
  const [barShown, setBarShown] = useState(true)
  const hideTimer = useRef<number | undefined>(undefined)
  // The bar's height band at the top of the wall: while the pointer is in
  // it the bar stays, so it never fades out under a cursor aiming at a button.
  const barRef = useRef<HTMLDivElement>(null)
  const poke = useCallback((e?: { clientY: number }) => {
    setBarShown(true)
    window.clearTimeout(hideTimer.current)
    const bar = barRef.current?.getBoundingClientRect()
    if (e && bar && e.clientY >= bar.top && e.clientY <= bar.bottom) return
    hideTimer.current = window.setTimeout(() => setBarShown(false), BAR_HIDE_MS)
  }, [])
  useEffect(() => {
    poke()
    return () => window.clearTimeout(hideTimer.current)
  }, [poke])
  const barVisible = barShown || paused || spot != null

  const exitTour = useCallback(() => navigate('/live'), [navigate])
  // A one-page tour has nothing to pause: no Pause button, no pill, and a
  // pause would only leave the bar stuck on screen.
  const canPause = pages.length > 1
  const togglePause = useCallback(() => { if (canPause) setPaused((p) => !p) }, [canPause])
  useEffect(() => { if (!canPause) setPaused(false) }, [canPause])

  useEffect(() => {
    const typing = (el: HTMLElement | null) =>
      !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable)
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null
      if (typing(el)) return
      if (e.ctrlKey || e.metaKey || e.altKey) return
      // Space always means pause, even with a bar or tile button focused
      // after a click; the button's own Space activation is cancelled on
      // keyup below, or one press would act twice.
      if (e.key === ' ') { e.preventDefault(); if (!e.repeat) togglePause() }
      else if (e.key === 'ArrowRight') go(1)
      else if (e.key === 'ArrowLeft') go(-1)
      else if (e.key === 'f' || e.key === 'F') toggleFs()
      else if (e.key === 'Escape') {
        // In fullscreen the browser takes Escape to leave it; out of it,
        // Escape lets go of a spotlit tile first, then leaves the tour.
        if (alerting) dismiss()
        else if (spot != null) setSpot(null)
        else if (!isFullscreen) exitTour()
      } else return
      poke()
    }
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.key === ' ' && (e.target as HTMLElement | null)?.tagName === 'BUTTON') e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('keyup', onKeyUp)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('keyup', onKeyUp)
    }
  }, [go, togglePause, toggleFs, exitTour, poke, spot, isFullscreen, alerting, dismiss])

  // Edit from the wall: the page on screen in the editor, which comes back here,
  // to that page.
  const location = useLocation()
  const { hasPermission } = usePermissions()
  const canEdit = !!tour.data?.editable && hasPermission('cameras.manage')
  const editPage = () => {
    if (!page) { navigate(`/tours/${id}/edit?return=${encodeURIComponent(location.pathname)}`); return }
    navigate(`/tours/${id}/edit?page=${page.index}&return=${encodeURIComponent(`${location.pathname}?page=${page.index}`)}`)
  }
  const openDisplay = () => window.open(`/display/tour/${id}`, '_blank', 'noopener')
  const cover = settings.fit === 'fill'

  const spec = page && tour.data ? tour.data.pages[page.index] : undefined
  const hidden = spec?.hidden_count ?? 0
  const pausedIds = useMemo(() => new Set(spec?.paused_camera_ids ?? []), [spec])

  let body: React.ReactNode
  if (!validId || (tour.isError && (tour.error as any)?.status === 404)) {
    body = (
      <WallMessage icon={<Route size={22} />} title={t('tours.notFound')}>
        <Link to="/live" className="text-xs px-2 py-1 border border-white/15 hover:bg-white/5">{t('tours.backToLive')}</Link>
      </WallMessage>
    )
  } else if (tour.isPending || !layoutsReady) {
    body = <div className="absolute inset-0 flex items-center justify-center"><div className="w-6 h-6 border-2 border-white/20 border-t-white/70 rounded-full animate-spin" /></div>
  } else if (!tour.data) {
    body = (
      <WallMessage icon={<AlertTriangle size={22} />} title={t('tours.loadFailed')} detail={t('tours.retrying')}>
        <button type="button" onClick={() => tour.refetch()} className="text-xs px-2 py-1 border border-white/15 hover:bg-white/5">{t('shared.retry')}</button>
      </WallMessage>
    )
  } else if (!tour.data.enabled) {
    body = <WallMessage icon={<PauseCircle size={22} />} title={t('tours.turnedOff')} detail={t('tours.turnedOffHint')} />
  } else if (!page && !alerting) {
    body = <WallMessage icon={<CameraOff size={22} />} title={t('tours.nothingToShow')} detail={t('tours.nothingToShowHint')} />
  } else {
    // What the wall shows, most urgent first: the alarming cameras, a
    // spotlit tile, or the tour's page.
    const alertBy = new Map(alerts.map((a) => [a.cameraId, a]))
    const spotCam = alerting ? null : spot
    const layout = alerting
      ? autoGrid(alerts.length)
      : spotCam != null ? autoGrid(1) : page!.layout
    const ids = alerting ? alerts.map((a) => a.cameraId) : spotCam != null ? [spotCam] : page!.cameraIds
    const labelClass = labelSize(layout.gridRows, display)
    const grid = (l: LayoutDefinition) => ({ gridTemplateColumns: `repeat(${l.gridCols}, minmax(0, 1fr))`, gridTemplateRows: `repeat(${l.gridRows}, minmax(0, 1fr))` })
    // Pages only, not an alarm or a spotlit camera: the next one started
    // early (hidden underneath) and the one fading out (on top).
    const plain = !alerting && spotCam == null
    const upNext = plain && warmStart && preloadKey && pages.length > 1 ? pages[wrapIndex(current, 1, pages.length)] : undefined
    const next = upNext && upNext.key === preloadKey && upNext.key !== page!.key && upNext.key !== leaving?.key ? upNext : undefined
    const out = plain && leaving && leaving.key !== page!.key ? leaving : undefined
    const pageTiles = (p: TourPage, shown: boolean) => p.layout.tiles.map((tile, i) => {
      const cid = p.cameraIds[i]
      const area = { gridRow: `${tile.row + 1} / span ${tile.rowSpan}`, gridColumn: `${tile.col + 1} / span ${tile.colSpan}` }
      const pausedHere = new Set(tour.data!.pages[p.index]?.paused_camera_ids ?? [])
      return cid != null ? (
        <div key={`cam-${cid}`} style={area} className="relative min-w-0 min-h-0">
          <TourTile cameraId={cid} cam={camById.get(cid)} pausedCamera={pausedHere.has(cid)} playing={visible} spotlit={false}
            cover={cover} labelClass={labelSize(p.layout.gridRows, display)} shown={shown}
            onDismissAlert={() => undefined} onSpotlight={() => setSpot(cid)} />
        </div>
      ) : (
        <div key={`empty-${i}`} style={area} className="bg-[#0b0f16]" />
      )
    })
    body = (
      <>
        {/* The page on screen. Keyed by page, so the page started early is
            this same element once it shows, already playing; within it,
            tiles are keyed by camera, so a spotlit or alarming camera that
            is on the page keeps its stream. */}
        <div key={page!.key} data-layer="current" className="absolute inset-0 z-10 grid gap-px bg-[#1a1f29]" style={grid(layout)}>
          {layout.tiles.map((tile, i) => {
            const cid = ids[i]
            const area = { gridRow: `${tile.row + 1} / span ${tile.rowSpan}`, gridColumn: `${tile.col + 1} / span ${tile.colSpan}` }
            return cid != null ? (
              <div key={`cam-${cid}`} style={area} className="relative min-w-0 min-h-0">
                <TourTile
                  cameraId={cid}
                  cam={camById.get(cid)}
                  pausedCamera={pausedIds.has(cid)}
                  playing={visible}
                  spotlit={spotCam === cid}
                  cover={cover}
                  labelClass={labelClass}
                  shown
                  alert={alertBy.get(cid)}
                  onDismissAlert={() => dismiss(cid)}
                  onSpotlight={() => setSpot((s) => (s === cid ? null : cid))}
                />
              </div>
            ) : (
              <div key={`empty-${i}`} style={area} className="bg-[#0b0f16]" />
            )
          })}
        </div>
        {/* Under the page on screen, which covers it: not opacity 0, as
            Chrome does not start a video it is told is invisible. */}
        {next && (
          <div key={next.key} data-layer="next" aria-hidden="true" inert className="absolute inset-0 z-0 pointer-events-none grid gap-px bg-[#1a1f29]" style={grid(next.layout)}>
            {pageTiles(next, false)}
          </div>
        )}
        {out && (
          <div key={out.key} data-layer="leaving" aria-hidden="true" inert className="absolute inset-0 z-20 pointer-events-none grid gap-px bg-[#1a1f29] tour-fade-out" style={grid(out.layout)}>
            {pageTiles(out, false)}
          </div>
        )}
      </>
    )
  }

  // "Page 2/5" counts only the pages that have something to show.
  const pageOrdinal = t('tours.pageOf', { n: current + 1, total: pages.length })
  const pageLabel = page ? (spec?.label || t('tours.pageN', { n: current + 1 })) : ''

  return (
    <section className={clsx(
      'flex flex-col bg-black overflow-hidden',
      // On the display route the tour is the whole page, nothing around it.
      display ? 'h-screen w-full' : 'h-[calc(100vh-5rem)] border border-[var(--border)]',
    )}>
      <div
        ref={wallRef}
        data-testid="tour-wall"
        className={clsx('relative flex-1 min-h-0 bg-black', !barVisible && 'cursor-none')}
        onMouseMove={poke}
      >
        {body}

        {/* Progress hairline: always there, so a guard can see the wall is turning. */}
        {page && pages.length > 1 && (
          <div className="pointer-events-none absolute top-0 inset-x-0 h-[2px] z-30 bg-white/5">
            <div
              key={pageKey}
              className="h-full bg-[var(--accent)] dash-tour-progress"
              style={{ animationDuration: `${page.durationMs}ms`, animationPlayState: running ? 'running' : 'paused' }}
            />
          </div>
        )}

        {/* The bar */}
        <div
          className={clsx(
            'absolute inset-x-0 top-0 z-20 flex items-center gap-2 px-3 pt-2 pb-6 text-white bg-gradient-to-b from-black/80 to-transparent transition-opacity duration-300',
            barVisible ? 'opacity-100' : 'opacity-0 pointer-events-none',
          )}
          ref={barRef}
          onFocus={() => poke()}
        >
          <Route size={14} className="shrink-0 text-white/70" aria-hidden="true" />
          <span className="min-w-0 shrink text-sm font-semibold truncate" data-testid="tour-name">{tour.data?.name ?? ''}</span>
          {page && (
            <span className="flex-1 flex items-center gap-2 min-w-0 overflow-hidden text-xs text-white/75">
              <span aria-hidden="true">·</span>
              <span className="truncate font-medium text-white" data-testid="tour-page-label">{pageLabel}</span>
              <span className="hidden sm:inline font-mono tabular-nums whitespace-nowrap">
                {pages.length > 1 && `${pageOrdinal} · `}
                {formatDuration(page.durationMs / 1000)}
              </span>
              {page.layoutFallback && (
                <span className="flex items-center gap-1 text-[var(--on-video-warn)]" title={t('tours.layoutMissingHint')}>
                  <AlertTriangle size={12} aria-hidden="true" />
                  <span className="hidden md:inline">{t('tours.layoutMissing')}</span>
                </span>
              )}
              {hidden > 0 && (
                <span className="hidden md:inline text-white/55 whitespace-nowrap">{t('tours.unavailable', { count: hidden })}</span>
              )}
            </span>
          )}
          <span className="ml-auto flex items-center gap-1 shrink-0">
            {pages.length > 1 && (
              <>
                <BarButton label={t('tours.previous')} onClick={() => go(-1)}><ChevronLeft size={15} /></BarButton>
                <BarButton label={paused ? t('tours.resume') : t('tours.pause')} onClick={togglePause} active={paused}>
                  {paused ? <Play size={14} /> : <Pause size={14} />}
                </BarButton>
                <BarButton label={t('tours.next')} onClick={() => go(1)}><ChevronRight size={15} /></BarButton>
                <span className="w-px h-4 bg-white/20 mx-1" aria-hidden="true" />
              </>
            )}
            {canEdit && (
              <BarButton label={t('tours.editPage')} onClick={editPage}><Pencil size={13} /></BarButton>
            )}
            {!display && (
              <BarButton label={t('tours.openAsDisplay')} onClick={openDisplay}><ExternalLink size={13} /></BarButton>
            )}
            <BarButton label={isFullscreen ? t('video.exitFullscreen') : t('live.fullscreen')} onClick={toggleFs}>
              {isFullscreen ? <Minimize size={14} /> : <Maximize size={14} />}
            </BarButton>
            <BarButton label={t('tours.exit')} onClick={exitTour}><X size={15} /></BarButton>
          </span>
        </div>

        {/* Alarm takeover banner: always on screen while it lasts. */}
        {alerting && (
          <div className="absolute left-1/2 -translate-x-1/2 top-3 z-40 flex items-center gap-3 pl-3 pr-1 py-1 text-white bg-[var(--danger)] shadow-lg max-w-[90%]" role="alert" data-testid="tour-alert">
            <BellRing size={15} className="shrink-0 animate-pulse" aria-hidden="true" />
            <span className="text-xs font-bold tracking-[0.18em] uppercase shrink-0">{t('tours.alert')}</span>
            <span className="text-sm font-medium truncate">
              {alerts[0].title} · {camById.get(alerts[0].cameraId)?.name ?? t('tours.cameraN', { id: alerts[0].cameraId })}
              {alerts.length > 1 && ` · ${t('tours.moreAlerts', { count: alerts.length - 1 })}`}
            </span>
            <span className="text-xs font-mono tabular-nums opacity-80 shrink-0">{fmt.time(alerts[0].at)}</span>
            <button type="button" onClick={() => dismiss()} className="shrink-0 px-2 py-0.5 text-xs border border-white/40 bg-black/20 hover:bg-black/40">
              {t('tours.backToTour')}
            </button>
          </div>
        )}

        {/* What comes next, always on screen: a guard should not need the
            mouse to know the wall is turning, or when. */}
        {page && pages.length > 1 && !alerting && !barVisible && (
          <NextChip
            deadline={deadline}
            heldLeftMs={heldLeftMs}
            held={!running}
            current={pageLabel}
            next={(() => {
              const at = wrapIndex(current, 1, pages.length)
              const label = tour.data?.pages[pages[at].index]?.label || t('tours.pageN', { n: at + 1 })
              // Pages sharing a label ("North" over three pages) say which one.
              return label === pageLabel ? `${label} · ${t('tours.pageOf', { n: at + 1, total: pages.length })}` : label
            })()}
            large={display}
          />
        )}

        {paused && page && pages.length > 1 && (
          <div className="pointer-events-none absolute left-1/2 -translate-x-1/2 bottom-4 z-20 px-3 py-1 text-[11px] font-bold tracking-[0.2em] uppercase text-black bg-[var(--on-video-warn)]" role="status">
            {t('tours.pausedPill')}
          </div>
        )}
      </div>
    </section>
  )
}

/** Camera-name size for a grid this many rows high, a size larger on a
 *  display board, which is read from across a room. */
function labelSize(rows: number, display: boolean): string {
  const steps = ['text-xs', 'text-[13px]', 'text-sm', 'text-base', 'text-lg', 'text-2xl']
  const base = rows <= 1 ? 3 : rows === 2 ? 2 : rows === 3 ? 1 : 0
  return steps[Math.min(steps.length - 1, base + (display ? 2 : 0))]
}

/** "North 1/3 → South in 18 s", ticking; held (paused, spotlit, hidden tab) it shows the time left, frozen. */
function NextChip({ deadline, heldLeftMs, held, current, next, large }: {
  deadline: number | null
  heldLeftMs: number
  held: boolean
  current: string
  next: string
  large: boolean
}) {
  const { t } = useTranslation()
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (held || deadline == null) return
    // Start from the real time, not the time the chip mounted: otherwise the
    // first tick reads a second longer than the page lasts.
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), 250)
    return () => window.clearInterval(id)
  }, [held, deadline])
  const leftMs = held || deadline == null ? heldLeftMs : Math.max(0, deadline - now)
  const secs = Math.ceil(leftMs / 1000)
  const soon = !held && secs <= 5
  return (
    <div
      className={clsx(
        'pointer-events-none absolute right-3 top-3 z-20 flex items-center gap-2 max-w-[calc(100%-1.5rem)] px-2.5 py-1 text-white border transition-colors',
        soon ? 'bg-[var(--accent)] border-transparent' : 'bg-black/65 border-white/10',
        large ? 'text-sm' : 'text-xs',
      )}
      data-testid="tour-next"
      aria-live="off"
    >
      <span className="font-medium truncate max-w-[14rem]">{current}</span>
      <span aria-hidden="true" className="opacity-70">→</span>
      <span className="truncate max-w-[14rem]">{next}</span>
      <span className="font-mono tabular-nums opacity-90">
        {held ? t('tours.heldFor', { left: formatDuration(secs) }) : t('tours.inSeconds', { left: formatDuration(secs) })}
      </span>
    </div>
  )
}

function BarButton({ label, onClick, active = false, children }: { label: string; onClick: () => void; active?: boolean; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      aria-pressed={active || undefined}
      className={clsx('p-1.5 border', active ? 'border-white/60 bg-white/20' : 'border-white/15 bg-black/40 hover:bg-black/70')}
    >
      {children}
    </button>
  )
}

function WallMessage({ icon, title, detail, children }: { icon: React.ReactNode; title: string; detail?: string; children?: React.ReactNode }) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 px-6 text-center text-white/70">
      {icon}
      <div className="text-sm font-medium text-white/85">{title}</div>
      {detail && <div className="text-xs max-w-md">{detail}</div>}
      {children}
    </div>
  )
}

/**
 * One tour tile. Plays the substream when the server offers one and it
 * works; any failure of the substream that is not an expired token (which
 * only needs a fresh token) moves this camera to the main stream for
 * SUB_RETRY_MS, after which a new page tries the substream again.
 */
function TourTile({ cameraId, cam, pausedCamera, playing, spotlit, cover, labelClass, shown, alert, onDismissAlert, onSpotlight }: {
  cameraId: number
  cam: CameraItem | undefined
  pausedCamera: boolean
  playing: boolean
  spotlit: boolean
  /** Crop to fill the tile (the tour's "Fill"). */
  cover: boolean
  /** Camera-name text size for this grid (labelSize). */
  labelClass: string
  /** False on a page started early (hidden) or fading out: no test ids, so
   *  only the page on screen answers for "the tiles". */
  shown: boolean
  /** Set while an alarm has this camera at the front of the wall. */
  alert?: ActiveAlert
  onDismissAlert: () => void
  onSpotlight: () => void
}) {
  const { t } = useTranslation()
  const fmt = useDateFormat()
  const qc = useQueryClient()
  const { version } = useCameraStatus(cameraId)
  const state = cam ? cameraState(cam) : 'degraded'
  const isPaused = pausedCamera || cam?.is_active === false
  const live = !isPaused && (state === 'online' || state === 'degraded')
  const [preferSub, setPreferSub] = useState(() => !subBlocked(cameraId))

  const info = useQuery({
    queryKey: streamKey(cameraId),
    enabled: playing && live,
    queryFn: () => fetchStream(cameraId),
    staleTime: STREAM_STALE_MS,
    gcTime: 50 * 60_000,
  })

  const lastRefresh = useRef(0)
  const onAuthExpired = useCallback(() => {
    const now = Date.now()
    if (now - lastRefresh.current < 10_000) return
    lastRefresh.current = now
    qc.invalidateQueries({ queryKey: streamKey(cameraId) })
  }, [qc, cameraId])

  const sub = info.data?.urls?.webrtc_sub
  const usingSub = preferSub && !!sub
  const whep = rebaseToCurrentOrigin(usingSub ? sub : info.data?.urls?.webrtc)
  const hls = rebaseToCurrentOrigin(info.data?.urls?.hls)

  const dropSub = useCallback(() => {
    NO_SUB.set(cameraId, Date.now() + SUB_RETRY_MS)
    setPreferSub(false)
  }, [cameraId])
  const onError = useCallback((msg: string) => {
    if (!usingSub || cam?.live_online === false) return
    if (/WHEP connection failed: 40[013]\b/.test(msg)) return // expired token, not a broken path
    dropSub()
  }, [usingSub, cam?.live_online, dropSub])

  // Backstop for a substream that neither plays nor errors: give it a
  // while, then use the main stream.
  const boxRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const box = boxRef.current
    if (!usingSub || !live || !playing || !box) return
    const timer = window.setTimeout(dropSub, SUB_STALL_MS)
    // Media events do not bubble, but they do pass through the capture phase.
    const onPlaying = () => window.clearTimeout(timer)
    box.addEventListener('playing', onPlaying, true)
    return () => {
      window.clearTimeout(timer)
      box.removeEventListener('playing', onPlaying, true)
    }
  }, [usingSub, live, playing, whep, version, dropSub])

  // Until the video plays, the camera's last frame stands in for it; while
  // it plays, that frame is kept fresh for the next time.
  const [started, setStarted] = useState(false)
  useEffect(() => { setStarted(false) }, [whep, version])
  useEffect(() => {
    const box = boxRef.current
    if (!live || !playing || !box) return
    let timer: number | undefined
    const grab = () => { const v = box.querySelector('video'); if (v) keepFrame(cameraId, v) }
    const onPlaying = () => {
      setStarted(true)
      window.clearInterval(timer)
      window.setTimeout(grab, 1_500)
      timer = window.setInterval(grab, LAST_FRAME_EVERY_MS)
    }
    box.addEventListener('playing', onPlaying, true)
    return () => {
      grab()
      window.clearInterval(timer)
      box.removeEventListener('playing', onPlaying, true)
    }
  }, [live, playing, cameraId, whep, version])
  const lastFrame = !started && live && playing ? LAST_FRAME.get(cameraId) : undefined

  const name = cam?.name || t('tours.cameraN', { id: cameraId })

  return (
    <div ref={boxRef} className="group absolute inset-0 bg-black overflow-hidden" data-testid={shown ? 'tour-tile' : undefined} data-camera-id={cameraId}>
      {live && playing && (whep || hls) ? (
        <VideoPlayer
          key={`${cameraId}-${version}-${usingSub ? 's' : 'm'}`}
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
          displayAspectOverride={cam?.display_aspect_ratio}
          showDetections={detectionsPreferred()}
          cameraId={cameraId}
          cover={cover}
          className="w-full h-full !bg-black"
        />
      ) : null}
      {lastFrame && (
        <img src={lastFrame} alt="" aria-hidden="true" draggable={false}
          className={clsx('pointer-events-none absolute inset-0 w-full h-full', cover ? 'object-cover' : 'object-contain')} />
      )}
      {live && playing && (whep || hls) ? null : (
        <div
          className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 text-white/45"
          style={{ background: 'repeating-linear-gradient(135deg, #07090d 0 10px, #0b0f16 10px 20px)' }}
        >
          {live ? (
            <div className="w-5 h-5 border-2 border-white/20 border-t-white/70 rounded-full animate-spin" />
          ) : (
            <>
              {isPaused ? <PauseCircle size={18} /> : <CameraOff size={18} />}
              <div className="text-[10px] font-mono uppercase tracking-[0.2em]">
                {isPaused ? t('tours.cameraPaused') : state === 'error' ? t('dashboard.statusError') : t('dashboard.statusOffline')}
              </div>
            </>
          )}
        </div>
      )}

      {/* Label band */}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t from-black/80 to-transparent" />
      <div className={clsx('pointer-events-none absolute left-2 bottom-1.5 flex items-center gap-1.5 text-white', alert ? 'right-11' : 'right-2')}>
        {/* An alarming tile says why on its own label; the banner above names the newest. */}
        {alert && (
          <span className="shrink-0 inline-flex items-center gap-1 max-w-[55%] px-1.5 py-0.5 bg-[var(--danger)] text-white text-xs font-semibold">
            <BellRing size={12} aria-hidden="true" />
            <span className="truncate">{alert.title}</span>
            <span className="font-mono tabular-nums font-normal opacity-85">{fmt.time(alert.at)}</span>
          </span>
        )}
        <span className="w-2 h-2 rounded-full shrink-0" style={{ background: isPaused ? 'var(--text-dim)' : STATE_INK[state] }} aria-hidden="true" />
        <span className={clsx(labelClass, 'font-semibold truncate [text-shadow:0_1px_2px_rgba(0,0,0,0.9)]')} data-testid={shown ? 'tour-tile-name' : undefined}>{name}</span>
        {cam?.location && (
          <span className="shrink-0 text-[0.8em] text-white/65 truncate max-w-[40%] [text-shadow:0_1px_2px_rgba(0,0,0,0.9)]">{cam.location}</span>
        )}
      </div>

      {/* Click the picture to spotlight it (the tour waits), click again to
          return. Not during an alarm: a stray click must not reshuffle it. */}
      {!alert && (
        <button
          type="button"
          onClick={onSpotlight}
          className="absolute inset-0 z-10 cursor-[inherit] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)] focus-visible:-outline-offset-2"
          aria-label={spotlit ? t('dashboard.backToWall') : t('tours.spotlight', { name })}
          title={spotlit ? t('dashboard.backToWall') : t('tours.spotlight', { name })}
        />
      )}

      {alert && (
        <>
          <div className="pointer-events-none absolute inset-0 z-10 ring-4 ring-inset ring-[var(--danger)]" aria-hidden="true" />
          <button
            type="button"
            onClick={onDismissAlert}
            className="absolute right-2 bottom-1.5 z-20 p-1 text-white bg-black/55 hover:bg-black/80 border border-white/20"
            aria-label={t('tours.dismissAlert', { name })}
            title={t('tours.dismissAlert', { name })}
          >
            <X size={13} />
          </button>
        </>
      )}
    </div>
  )
}
