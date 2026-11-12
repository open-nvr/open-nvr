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

import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useTranslation, useDateFormat, type DateFormatters } from '../i18n'
import { useConfirm } from '../components/ui/ConfirmDialog'
import { apiService } from '../lib/apiService'
import { queryClient, useCameras, useMediaMtxHealth } from '../lib/queries'
import { useCameraStatusConnected } from '../hooks/useCameraStatus'
import { extractApiError } from '../lib/apiError'
import { useSnackbar } from '../components/Snackbar'
import { usePermissions } from '../hooks/usePermissions'
import { Camera as CameraIcon, CameraOff, Download, LayoutGrid, List, PauseCircle, Pencil, Play, PlayCircle, Plus, Search as SearchIcon, Trash2, Unplug, Upload, Video, Webcam } from 'lucide-react'
import { Badge, Button, EmptyState, Skeleton, Table, THead, TBody, TR, TH, TD } from '../components/ui'
import { Checkbox, Field, Input, Select } from '../components/ui/form'
import { IconButton, PageTitleBar, TITLE_ACTION_CLASS, TITLE_PANEL_BODY_CLASS } from '../components/ui/layout'
import { Pagination } from '../components/ui/Pagination'
import type { BadgeVariant } from '../components/ui'
import { AddCameraDialog } from '../components/AddCameraDialog'
import { CameraExportDialog, CameraImportDialog } from '../components/cameras/CameraImportExport'
import { RecordingPauseDialog } from '../components/cameras/RecordingPauseDialog'
import { CameraBulkMenu, type BulkAction } from '../components/cameras/CameraBulkMenu'
import { CameraThumb } from '../components/live/CameraThumb'
import { LivePeek } from '../components/live/LivePeek'
import { QrScanner } from '../components/QrScanner'
import { parseCameraQr } from '../lib/cameraQr'
import { ASPECT_OPTIONS, isCustomAspect } from '../lib/aspect'
import { parseRtspUrl, rtspPortFromUrl, syncCameraIdentity } from '../lib/cameraIdentity'
import type { IdentityField } from '../lib/cameraIdentity'
import { formatDuration } from '../lib/time'
import { stopSession } from '../lib/webcamSessions'
import { Modal } from '../components/Modal'
import { HostWebcamPicker, useHostAgent } from '../components/HostWebcamPicker'
import { describeStream } from '../lib/hostAgent'

// Per-camera capability assignment ("camera 1 does LPR") — slice 1 of
// docs/design/per-camera-assignment.md. Written only here (the camera
// settings surface); consumers read it from the internal endpoint.
type CameraAssignment = { skill: string; labels?: string[] | null }

/** The Location filter's value for cameras that have none (any string a
 *  location cannot be). */
const NO_LOCATION = '\u0000none'

type CameraStatusFilter = 'all' | 'active' | 'inactive'
type CameraView = 'table' | 'grid'
const STATUS_PREF = 'opennvr.cameras.status'
const VIEW_PREF = 'opennvr.cameras.view'
const TILE_PREF = 'opennvr.cameras.tile'
type TileSize = 's' | 'm' | 'l'
/** Smallest tile width per size; the grid fits as many as the width allows. */
const TILE_MIN_PX: Record<TileSize, number> = { s: 180, m: 220, l: 300 }

/** A remembered choice, or the default when storage is empty, blocked or
 *  holds something this version does not know. */
function readPref<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const v = window.localStorage.getItem(key)
    return v && (allowed as readonly string[]).includes(v) ? (v as T) : fallback
  } catch {
    return fallback
  }
}

function writePref(key: string, value: string) {
  try { window.localStorage.setItem(key, value) } catch { /* private mode: not remembered */ }
}

type CameraLocations = { locations: Array<{ location: string; count: number }>; none: number }

type Camera = {
  id: number
  name: string
  description?: string | null
  ip_address: string
  port: number
  username?: string | null
  password?: string | null
  rtsp_url?: string | null
  substream_url?: string | null
  /** "publisher" = a webcam: its video is pushed into the path (browser or
   *  host helper), so it has no address, URL or credentials. */
  source_type?: string | null
  /** For a USB webcam on the OpenNVR server: the host agent's device id. */
  source_device?: string | null
  /** Display-only aspect override; null = auto-detect. See lib/aspect.ts. */
  display_aspect_ratio?: string | null
  location?: string | null
  vlan?: string | null
  status?: string | null
  owner_id: number
  is_active: boolean
  // Tier-0 object detection; false = off (e.g. switched off from Home
  // Assistant). The camera keeps streaming and recording either way.
  detection_enabled?: boolean
  deleted_at?: string | null
  mediamtx_provisioned?: boolean | null
  // Live connectivity as tracked by the recorder (MediaMTX path ready AND
  // bytes flowing). null/undefined means UNKNOWN — not offline. See
  // streamState below for why that distinction has to survive to the badge.
  live_online?: boolean | null
  // Configuration intent — always true for a provisioned camera and not
  // switchable, so it says nothing about whether footage is being written.
  recording_enabled?: boolean | null
  // Observed recording health, derived server-side from the newest indexed
  // segment. Absent (undefined) means the endpoint didn't compute it.
  recording_state?: 'recording' | 'not_recording' | 'stalled' | 'degraded' | 'never' | 'off' | null
  last_recording_at?: string | null
  // Set while recording is deliberately paused; resume_at null = until resumed.
  recording_pause?: { since: string; resume_at: string | null; by: string; reason: string | null } | null
  /** The escalation proposal (weak evidence on the sub stream → detect on main?) or the decision in force. */
  escalation?: { status: 'proposed' | 'accepted' | 'dismissed'; reasons: string[]; review_due: boolean } | null
  // ONVIF device metadata
  manufacturer?: string | null
  model?: string | null
  firmware_version?: string | null
  serial_number?: string | null
  hardware_id?: string | null
  assignments?: CameraAssignment[] | null
}

/** Which preset the stored display-aspect value corresponds to. A stored
 *  ratio that isn't one of the presets lands on 'custom'. */
function aspectChoiceOf(stored: string | null | undefined): string {
  if (!stored) return 'auto'
  if (isCustomAspect(stored)) return 'custom'
  return stored
}

/** The value to persist: null means "auto", which is how the API and the DB
 *  both spell "no override". */
function aspectValueOf(form: CameraForm): string | null {
  const choice = form.display_aspect_choice || 'auto'
  if (choice === 'auto') return null
  if (choice === 'custom') return form.display_aspect_custom?.trim() || null
  return choice
}

type CameraForm = {
  name: string
  /** A USB webcam camera's device (host agent id). */
  source_device?: string
  description?: string
  ip_address: string
  port: number
  username?: string
  password?: string
  rtsp_url?: string
  substream_url?: string
  /** Preset key: 'auto' | 'native' | '16:9' | '4:3' | 'custom'. */
  display_aspect_choice?: string
  /** Free-form "W:H", only meaningful while display_aspect_choice is 'custom'. */
  display_aspect_custom?: string
  location?: string
  vlan?: string
  status?: string
  is_active?: boolean
  detection_enabled?: boolean
}

export function Cameras() {
  const fmt = useDateFormat()
  const navigate = useNavigate()
  const { t } = useTranslation()
  const { hasPermission, isSuperuser } = usePermissions()
  const canManageCameras = hasPermission('cameras.manage')
  // Pausing needs recordings.pause AND the site's opt-in; resuming is open
  // to camera managers too, so nobody is stuck with a paused camera.
  const canPause = hasPermission('recordings.pause')
  const canResume = canPause || canManageCameras
  const siteInfo = useQuery({
    queryKey: ['site-info'],
    queryFn: async () => (await apiService.getSiteInfo()).data as { recording_pause_enabled?: boolean },
    staleTime: 60_000,
    enabled: canPause,
  })
  const pauseAllowed = siteInfo.data?.recording_pause_enabled === true
  // An admin sees Pause even while it is off, and is pointed at the setting.
  const showPause = canPause && (pauseAllowed || isSuperuser)
  const [pausing, setPausing] = useState<Array<{ id: number; name: string }> | null>(null)
  const [showBulkLocation, setShowBulkLocation] = useState(false)
  const [bulkLocation, setBulkLocation] = useState('')
  const { showError, showSuccess, showInfo, showWarning } = useSnackbar()
  const confirm = useConfirm()
  // A mutation (delete / bulk op) is in flight. Distinct from the list's own
  // fetching state: the list now refreshes on its own in the background, and
  // that must never disable the buttons.
  const [mutating, setMutating] = useState(false)
  // All / Active / Inactive, and table or grid: both remembered per browser.
  const [status, setStatus] = useState<CameraStatusFilter>(() => readPref(STATUS_PREF, ['all', 'active', 'inactive'], 'all'))
  const [view, setView] = useState<CameraView>(() => readPref(VIEW_PREF, ['table', 'grid'], 'table'))
  const pickStatus = (s: CameraStatusFilter) => { setPage(1); setStatus(s); writePref(STATUS_PREF, s) }
  const pickView = (v: CameraView) => { setView(v); writePref(VIEW_PREF, v) }
  const [tile, setTile] = useState<TileSize>(() => readPref(TILE_PREF, ['s', 'm', 'l'], 'm'))
  const pickTile = (v: TileSize) => { setTile(v); writePref(TILE_PREF, v) }
  const activeOnly = status === 'active'
  const [limit, setLimit] = useState(20)
  const [page, setPage] = useState(1)
  const skip = useMemo(() => (page - 1) * limit, [page, limit])
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  // '' every location; NO_LOCATION the cameras without one; else one location.
  const [location, setLocation] = useState('')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  // Every camera matching the filters, fetched when "Select all N" is taken,
  // so bulk actions know the state of cameras on other pages too.
  const [allPages, setAllPages] = useState<Camera[] | null>(null)
  // The camera being watched live in a dialog over this page.
  const [peek, setPeek] = useState<Camera | null>(null)
  const [selectingAll, setSelectingAll] = useState(false)
  const liveUpdates = useCameraStatusConnected()

  // One request per pause in typing, not one per keystroke.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query), 250)
    return () => clearTimeout(t)
  }, [query])

  const camsQuery = useCameras({
    skip,
    limit,
    status,
    q: debouncedQuery || undefined,
    ...(location === NO_LOCATION ? { no_location: true } : location ? { location } : {}),
  })
  const locationsQuery = useQuery({
    queryKey: ['camera-locations', activeOnly],
    queryFn: async () => (await apiService.getCameraLocations({ active_only: activeOnly })).data as CameraLocations,
    staleTime: 60_000,
  })
  const locations = locationsQuery.data
  const cameras = useMemo<Camera[]>(
    () => (camsQuery.data?.cameras ?? []) as Camera[],
    [camsQuery.data],
  )
  const total = camsQuery.data?.total ?? 0
  // USB webcams are streamed by the host agent: its view names their
  // webcam and says how each stream is doing. Asked only when needed.
  const hasUsbWebcams = cameras.some((c) => c.source_type === 'publisher' && !!c.source_device)
  const { info: hostAgent } = useHostAgent(hasUsbWebcams && canManageCameras)
  const counts = camsQuery.data?.counts

  const mtxQuery = useMediaMtxHealth()
  // null while we genuinely don't know yet — the "Media Server is not running"
  // banner must not flash during the first round trip.
  const mediamtxAvailable = mtxQuery.isPending ? null : mtxQuery.data?.status === 'ok'

  useEffect(() => {
    if (camsQuery.isError) {
      showError(extractApiError(camsQuery.error, t('dashboard.failedCameras')))
    }
  }, [camsQuery.isError, camsQuery.error, showError, t])

  // A selection is scoped to what is currently listed, so changing the listing
  // drops it. Deliberately keyed on the view parameters and not on the data:
  // the list refetches itself every minute now, and wiping a half-made
  // selection on a background refresh would be maddening.
  useEffect(() => {
    setSelected(new Set())
    setAllPages(null)
  }, [limit, status, debouncedQuery, location])
  // Paging keeps a selection made with "Select all N" (it spans the pages by
  // design) and drops a page-only one, as before.
  const spansPagesRef = useRef(false)
  useEffect(() => {
    if (!spansPagesRef.current) setSelected(new Set())
  }, [skip])

  const [showImport, setShowImport] = useState(false)
  const [showExport, setShowExport] = useState(false)

  // Bulk assign state
  const [showBulkAssign, setShowBulkAssign] = useState(false)
  const [bulkUserId, setBulkUserId] = useState<number | ''>('')
  const [userQuery, setUserQuery] = useState('')
  const [userOptions, setUserOptions] = useState<Array<{ id: number; username: string; email: string; is_active: boolean }>>([])
  const [usersLoading, setUsersLoading] = useState(false)
  const [bulkCanView, setBulkCanView] = useState(true)
  const [bulkCanManage, setBulkCanManage] = useState(false)

  // Edit/Create state
  const [editing, setEditing] = useState<Camera | null>(null)
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [showEditDialog, setShowEditDialog] = useState(false)
  // Dedicated to the edit form, so a delete elsewhere on the page cannot
  // disable the Update button and make it look broken.
  const [saving, setSaving] = useState(false)
  const [scanQr, setScanQr] = useState(false)

  const [form, setForm] = useState<CameraForm>({
    name: '',
    description: '',
    ip_address: '',
    port: 554,
    username: '',
    password: '',
    rtsp_url: '',
    substream_url: '',
    display_aspect_choice: 'auto',
    display_aspect_custom: '',
    location: '',
    vlan: '',
    status: 'unknown',
  })

  const resetForm = () => setForm({
    name: '',
    description: '',
    ip_address: '',
    port: 554,
    username: '',
    password: '',
    rtsp_url: '',
    substream_url: '',
    display_aspect_choice: 'auto',
    display_aspect_custom: '',
    location: '',
    vlan: '',
    status: 'unknown',
  })

  // Observed stream state. `live_online` is tracked by the recorder off
  // MediaMTX's ready/not-ready hooks and arrives on the camera row itself —
  // this used to cost one /mediamtx-status probe per camera per page load,
  // three MediaMTX round trips each, and still went stale the moment a camera
  // dropped because nothing refetched it.
  const streamState = (c: Camera): { variant: BadgeVariant; label: string; title?: string; icon?: boolean } => {
    if (mediamtxAvailable === false) {
      return { variant: 'warning', label: 'Disconnected', title: 'Media Server is not running', icon: true }
    }
    // A paused camera has no path at all, so it is neither live nor broken.
    // Checked before the unknown case below, which it would otherwise fall
    // into and sit on "Checking…" forever.
    if (!c.is_active) {
      return { variant: 'neutral', label: '—', title: 'Camera is paused' }
    }
    if (c.live_online === true) return { variant: 'success', label: 'Online' }
    if (c.live_online === false) {
      return { variant: 'warning', label: 'Disconnected', title: 'Stream not receiving data' }
    }
    // Unknown, NOT offline: the recorder keeps this state in memory and
    // re-seeds it a short while after starting. Claiming "Disconnected" here
    // would show the whole fleet as down every time the server restarts.
    if (c.mediamtx_provisioned === true) {
      return { variant: 'neutral', label: 'Checking…', title: 'Live state not yet known' }
    }
    if (c.mediamtx_provisioned === false) return { variant: 'destructive', label: 'Error' }
    return { variant: 'neutral', label: 'Not configured' }
  }

  // Observed recording health, not the config flag. `recording_enabled` is
  // true for every provisioned camera and cannot be switched off, so it used
  // to claim "Recording" beside a dead stream. The server derives this from
  // the newest written segment and the stream's live state, using the
  // recording watchdog's own thresholds, so this badge agrees both with the
  // stall alert and with the Stream column beside it.
  const recordingState = (c: Camera): { variant: BadgeVariant; label: string; title?: string } => {
    const at = c.last_recording_at ? new Date(c.last_recording_at) : null
    const agoSeconds = at ? Math.max(0, (Date.now() - at.getTime()) / 1000) : null
    const seenAt = at ? `Last segment ${fmt.dateTime(at)}` : undefined

    // Deliberately stopped: say so, and by whom, rather than a bare "Off".
    const pause = c.recording_pause
    if (pause && c.is_active) {
      const title = [
        t('recordingPause.pausedBy', { by: pause.by, since: fmt.dateTime(pause.since) }),
        pause.resume_at ? t('recordingPause.resumesAt', { at: fmt.dateTime(pause.resume_at) }) : t('recordingPause.untilResumed'),
        pause.reason,
      ].filter(Boolean).join(' · ')
      return { variant: 'warning', label: t('recordingPause.paused'), title }
    }

    switch (c.recording_state) {
      case 'recording':
        return { variant: 'success', label: 'Recording', title: seenAt }
      case 'not_recording':
        // The source is down but the last segment is too recent for the
        // watchdog to call it stalled. Saying "Recording" here is what put
        // this badge in direct contradiction with a Disconnected stream.
        return {
          variant: 'warning',
          label: 'Not recording',
          title: seenAt ? `${seenAt} — stream is down, nothing is being written` : 'Stream is down, nothing is being written',
        }
      case 'stalled': {
        // formatDuration never rolls up to days, so a multi-day stall would
        // render "Stalled 74h 12m" and overflow the column.
        const age =
          agoSeconds === null ? null : agoSeconds >= 86400 ? '>24h' : formatDuration(agoSeconds)
        return {
          variant: 'warning',
          label: age ? `Stalled ${age}` : 'Stalled',
          title: seenAt ? `${seenAt} — nothing written since` : undefined,
        }
      }
      case 'degraded':
        // Segments keep landing, so the recorder looks alive, but they hold
        // no playable video: the stream's timestamps stopped advancing
        // (camera clock wrap behind a relay). The segment-health check
        // raised recording_degraded; this is that alarm on the badge.
        return {
          variant: 'destructive',
          label: 'Degraded',
          title: seenAt
            ? `${seenAt} — segments arrive but hold no playable video; restart the camera stream`
            : 'Segments arrive but hold no playable video; restart the camera stream',
        }
      case 'never':
        return { variant: 'warning', label: 'No data', title: 'No recording has ever been indexed for this camera' }
      case 'off':
        return { variant: 'neutral', label: 'Off' }
      default:
        // Endpoints that don't compute it leave this unset — say so rather
        // than implying recording is off.
        return { variant: 'neutral', label: '—', title: 'Recording state unavailable' }
    }
  }

  // One camera's buttons, shared by the table row and the grid card.
  const cameraActions = (c: Camera) => (
    <>
      {canManageCameras && c.source_type === 'publisher' && !c.source_device && (
        <IconButton
          variant="outline"
          onClick={() => navigate(`/cameras/${c.id}/publish`)}
          title="Publish a webcam to this camera"
          label={`Publish a webcam to ${c.name}`}
        >
          <Webcam size={15} />
        </IconButton>
      )}
      {canManageCameras && (
        <IconButton variant="outline" onClick={() => startEdit(c)} title={t('camera.edit')} label={`${t('camera.edit')} ${c.name}`}>
          <Pencil size={15} />
        </IconButton>
      )}
      {/* Recording is always on unless the site allows
          pausing (Settings > Recording); then a pause is
          timed or until resumed, and audited. */}
      {c.is_active && c.recording_pause && canResume && (
        <IconButton
          variant="outline"
          className="!text-[var(--warning)] !border-[var(--warning)]"
          onClick={() => onResumeRecording(c)}
          disabled={mutating}
          title={t('recordingPause.resume')}
          label={`${t('recordingPause.resume')} ${c.name}`}
          data-testid={`recording-resume-${c.id}`}
        >
          <PlayCircle size={15} />
        </IconButton>
      )}
      {c.is_active && !c.recording_pause && showPause && c.mediamtx_provisioned === true && (
        <IconButton
          variant="outline"
          onClick={() => setPausing([c])}
          title={t('recordingPause.pause')}
          label={`${t('recordingPause.pause')} ${c.name}`}
          data-testid={`recording-pause-${c.id}`}
        >
          <PauseCircle size={15} />
        </IconButton>
      )}
      {c.mediamtx_provisioned === true && (
        <IconButton
          variant="outline"
          className="!text-[var(--accent)] !border-[color-mix(in_oklab,var(--accent)_50%,var(--border))]"
          onClick={() => setPeek(c)}
          title={t('camera.viewLive')}
          label={`View ${c.name} live`}
        >
          <Video size={15} />
        </IconButton>
      )}
      {canManageCameras && (
        <IconButton
          variant="outline"
          className="hover:!text-[var(--danger)] hover:!border-[var(--danger)]"
          onClick={() => onDelete(c)}
          title={t('camera.delete')}
          label={`Delete ${c.name}`}
        >
          <Trash2 size={15} />
        </IconButton>
      )}
    </>
  )

  const activeBadge = (c: Camera) => c.is_active
    ? <Badge variant="success">{t('cameraBulk.status_active')}</Badge>
    : <Badge variant="neutral">{t('cameraBulk.status_inactive')}</Badge>

  const toggleSelected = (id: number, on: boolean) => {
    const next = new Set(selected)
    if (on) next.add(id); else next.delete(id)
    setSelected(next)
  }

  const pageSelectedCount = cameras.filter((c) => selected.has(c.id)).length
  const pageAllSelected = cameras.length > 0 && pageSelectedCount === cameras.length
  const everythingSelected = allPages !== null && total > 0 && selected.size >= total
  spansPagesRef.current = everythingSelected

  /** Header checkbox: tick or clear this page. Clearing while everything is
   *  selected clears everything, as the box then stands for the whole set. */
  const selectPage = (on: boolean) => {
    if (!on && everythingSelected) { clearSelection(); return }
    const next = new Set(selected)
    for (const c of cameras) { if (on) next.add(c.id); else next.delete(c.id) }
    setSelected(next)
  }

  const clearSelection = () => { setSelected(new Set()); setAllPages(null) }

  const selectAllPages = async () => {
    setSelectingAll(true)
    try {
      const { data } = await apiService.getCameras({
        status,
        ...(debouncedQuery ? { q: debouncedQuery } : {}),
        ...(location === NO_LOCATION ? { no_location: true } : location ? { location } : {}),
      })
      const list = (data?.cameras ?? []) as Camera[]
      setAllPages(list)
      setSelected(new Set(list.map((c) => c.id)))
    } catch (e: any) {
      showError(extractApiError(e, t('cameraBulk.selectAllFailed')))
    } finally {
      setSelectingAll(false)
    }
  }

  // "All 10 on this page selected. Select all 20" and "All 20 selected.
  // Clear selection", in the selection strip above the list.
  const selectionNotice = () => {
    if (selected.size === 0) return null
    // Extending a ticked page to every page, while there are more pages.
    const offerAll = pageAllSelected && !everythingSelected && total > cameras.length
    return (
      <span className="text-[var(--text-dim)]">
        {everythingSelected && total > cameras.length
          ? t('cameraBulk.allSelected', { count: total })
          : t('cameraBulk.selected', { count: selected.size })}
        {offerAll && (
          <>
            {' '}
            <button
              type="button"
              className="text-[var(--accent)] hover:underline disabled:opacity-60"
              onClick={selectAllPages}
              disabled={selectingAll}
              data-testid="camera-select-all-pages"
            >
              {t('cameraBulk.selectAllPages', { count: total })}
            </button>
            <span aria-hidden="true"> · </span>
          </>
        )}
        {!offerAll && ' '}
        <button
          type="button"
          className="text-[var(--accent)] hover:underline"
          onClick={clearSelection}
          data-testid="camera-clear-selection"
        >
          {t('cameraBulk.clearSelection')}
        </button>
      </span>
    )
  }

  const pageCheckbox = (testId: string) => (
    <input
      type="checkbox"
      className="accent-[var(--accent)]"
      checked={pageAllSelected}
      ref={(el) => { if (el) el.indeterminate = pageSelectedCount > 0 && !pageAllSelected }}
      onChange={(e) => selectPage(e.target.checked)}
      aria-label={t('cameraBulk.selectPage')}
      data-testid={testId}
    />
  )

  // Selection strip, the list's top edge in both views: what is selected on
  // the left, what to do with it on the right. Always there and of fixed
  // height, so selecting moves nothing on the page. Its box sits in line with
  // the list's own: the table's checkbox column (12px cell padding), or the
  // cards' boxes (8px in, 24px wide).
  const renderSelectionBar = () => (
    <div
      className={`flex h-10 shrink-0 items-center gap-2 border border-[var(--border)] bg-[var(--panel)] pr-2 text-[13px] ${view === 'grid' ? 'pl-2 mb-2' : 'pl-3 border-b-0'}`}
      data-testid="camera-selection-bar"
    >
      <label className={`inline-flex shrink-0 items-center leading-none cursor-pointer ${view === 'grid' ? 'gap-0.5' : 'gap-2'}`}>
        {view === 'grid'
          ? <span className="flex h-6 w-6 items-center justify-center">{pageCheckbox('camera-select-page')}</span>
          : pageCheckbox('camera-select-page')}
        {t('cameraBulk.selectPage')}
      </label>
      <span className="min-w-0 truncate">{selectionNotice()}</span>
      <div className="ml-auto flex shrink-0 items-center gap-3">
        {(canManageCameras || canPause || canResume) && selected.size > 0 && (
          <CameraBulkMenu
            canManage={canManageCameras}
            canPause={showPause}
            canResume={canResume}
            disabled={mutating}
            onPick={onBulkAction}
          />
        )}
        {/* Tile size belongs to the grid, so it sits on the grid's strip,
            small and quiet, rather than beside the Table/Grid switch. */}
        {view === 'grid' && (
          <div role="radiogroup" aria-label={t('cameraBulk.tileSize')} className="inline-flex items-center gap-0.5">
            {(['s', 'm', 'l'] as const).map((v) => (
              <button
                key={v}
                type="button"
                role="radio"
                aria-checked={tile === v}
                onClick={() => pickTile(v)}
                title={t(`cameraBulk.tile_${v}`)}
                aria-label={t(`cameraBulk.tile_${v}`)}
                data-testid={`camera-tile-${v}`}
                className={`h-6 w-6 text-[11px] font-medium transition-colors ${
                  tile === v
                    ? 'text-[var(--accent)] underline underline-offset-4 decoration-2'
                    : 'text-[var(--text-dim)] hover:text-[var(--text)]'
                }`}
              >
                {v.toUpperCase()}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )

  // Grid view: one card per camera with a still of what it sees. Stills load
  // as cards scroll into view and are cached five minutes (CameraThumb).
  const renderGrid = () => (
    <div className={`min-h-0 overflow-auto ${camsQuery.isFetching || mutating ? 'opacity-60 transition-opacity' : 'transition-opacity'}`}>
      <div
        className={`grid ${tile === 's' ? 'gap-2' : 'gap-3'}`}
        style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${TILE_MIN_PX[tile]}px, 1fr))` }}
        data-testid="camera-grid"
      >
        {cameras.map((c) => {
          const stream = streamState(c)
          const rec = recordingState(c)
          const device = [c.manufacturer, c.model].filter(Boolean).join(' ')
          const isSelected = selected.has(c.id)
          return (
            <div
              key={c.id}
              data-testid={`camera-card-${c.id}`}
              className={`flex flex-col border bg-[var(--panel)] ${isSelected ? 'border-[var(--accent)]' : 'border-[var(--border)]'} ${c.is_active ? '' : 'opacity-70'}`}
            >
              <div className="group relative aspect-video bg-[#0b0f16]">
                {/* Clicking the picture selects the card, like a row's
                    checkbox; watching live is the Live button in its corner,
                    kept out of the middle so a click there never opens it. */}
                <button
                  type="button"
                  className="absolute inset-0 block cursor-pointer"
                  onClick={() => toggleSelected(c.id, !isSelected)}
                  aria-pressed={isSelected}
                  aria-label={`Select ${c.name}`}
                  data-testid={`camera-card-select-${c.id}`}
                />
                {c.is_active && c.mediamtx_provisioned === true ? (
                  <>
                    <CameraThumb cameraId={c.id} alt={c.name} className="pointer-events-none absolute inset-0" />
                    <button
                      type="button"
                      onClick={() => setPeek(c)}
                      title={t('camera.viewLive')}
                      aria-label={`View ${c.name} live`}
                      data-testid={`camera-card-live-${c.id}`}
                      className="absolute right-2 bottom-2 inline-flex items-center gap-1 bg-black/65 px-2 py-0.5 text-[11px] text-white hover:bg-[var(--accent)] focus-visible:bg-[var(--accent)]"
                    >
                      <Play size={11} aria-hidden="true" /> {t('cameraBulk.live')}
                    </button>
                  </>
                ) : (
                  <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-1 text-white/40 text-xs">
                    <CameraOff size={20} aria-hidden="true" />
                    {c.is_active ? stream.label : t('cameraBulk.status_inactive')}
                  </div>
                )}
                {isSelected && <div className="pointer-events-none absolute inset-0 ring-2 ring-inset ring-[var(--accent)]" />}
                <label className="absolute left-2 top-2 flex h-6 w-6 items-center justify-center bg-black/55 cursor-pointer">
                  <input
                    type="checkbox"
                    className="accent-[var(--accent)]"
                    checked={isSelected}
                    onChange={(e) => toggleSelected(c.id, e.target.checked)}
                    aria-label={`Select ${c.name}`}
                  />
                </label>
                <div className="pointer-events-none absolute right-2 top-2 flex gap-1">
                  <Badge variant={stream.variant} title={stream.title}>
                    {stream.icon && <Unplug size={12} />}
                    {stream.label}
                  </Badge>
                </div>
                <div className="pointer-events-none absolute left-2 bottom-2">
                  <Badge variant={rec.variant} title={rec.title}>{rec.label}</Badge>
                </div>
              </div>
              <div className="flex min-w-0 flex-1 flex-col gap-0.5 px-3 pt-2">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-sm font-medium" title={c.name}>{c.name}</span>
                  {c.is_active && c.detection_enabled === false && (
                    <Badge variant="warning" className="shrink-0" title={t('cameras.detectionOffHint')}>{t('cameras.detectionOff')}</Badge>
                  )}
                  {c.escalation?.status === 'proposed' && (
                    <Badge variant="warning" className="shrink-0" title={t('cameras.escalationProposedHint')}>{t('cameras.escalationProposed')}</Badge>
                  )}
                  {c.escalation?.status === 'accepted' && (
                    <Badge variant={c.escalation.review_due ? 'warning' : 'info'} className="shrink-0"
                           title={c.escalation.review_due ? t('cameras.escalationReviewHint') : t('cameras.escalationAcceptedHint')}>
                      {c.escalation.review_due ? t('cameras.escalationReview') : t('cameras.escalationAccepted')}
                    </Badge>
                  )}
                </div>
                <div className="truncate text-xs text-[var(--text-dim)]" title={c.location || undefined}>
                  {c.location?.trim() || '—'}
                </div>
                {device && tile !== 's' && <div className="truncate text-[11px] text-[var(--text-dim)]" title={device}>{device}</div>}
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                {activeBadge(c)}
                <div className="flex items-center gap-1">{cameraActions(c)}</div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )

  // User search for bulk assign
  useEffect(() => {
    let alive = true
    const run = async () => {
      if (!userQuery) { setUserOptions([]); return }
      try {
        setUsersLoading(true)
        const { data } = await apiService.getUsers({ q: userQuery, limit: 10, active_only: true })
        const list = Array.isArray(data.users) ? data.users : data
        if (alive) setUserOptions(list)
      } catch {
        if (alive) setUserOptions([])
      } finally {
        if (alive) setUsersLoading(false)
      }
    }
    const t = setTimeout(run, 250)
    return () => { alive = false; clearTimeout(t) }
  }, [userQuery])

  // Every mutation funnels through here. Invalidating the key rather than
  // refetching this component's params keeps the Dashboard's copy of the list
  // honest too, since both read the same cache.
  const refreshCameras = () => {
    // An edit can change a camera's location, so the filter's menu too.
    queryClient.invalidateQueries({ queryKey: ['camera-locations'] })
    return queryClient.invalidateQueries({ queryKey: ['cameras'] })
  }

  // A camera's address, port and credentials live twice over: in these fields
  // and again inside the RTSP URL. Re-sync when the operator leaves a control
  // rather than on every keystroke — mid-type, "192.168.1." is not a host and a
  // half-pasted URL is unparseable, so syncing per character would fight them.
  const syncIdentity = (changed: IdentityField) =>
    setForm((prev) => syncCameraIdentity(prev, changed))

  // The URL's host when it contradicts the IP field, else null. Drives the
  // warning under the URL input.
  const urlHostMismatch = useMemo(() => {
    const host = parseRtspUrl(form.rtsp_url)?.hostname
    return host && form.ip_address && host !== form.ip_address ? host : null
  }, [form.rtsp_url, form.ip_address])

  const onUpdate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!editing) return
    try {
      setSaving(true)
      const payload: any = {
        name: form.name,
        description: form.description || null,
        ip_address: form.ip_address,
        port: Number(form.port) || undefined,
        username: form.username || null,
        ...(form.password ? { password: form.password } : {}),
        rtsp_url: form.rtsp_url || null,
        substream_url: form.substream_url || null,
        display_aspect_ratio: aspectValueOf(form),
        location: form.location || null,
        vlan: form.vlan || null,
        status: form.status || undefined,
        is_active: form.is_active,
        detection_enabled: form.detection_enabled !== false,
      }
      if (editing.source_type === 'publisher') {
        // A webcam has none of these; sending the empty form values would
        // only ask the server to strip them again.
        for (const k of ['ip_address', 'port', 'username', 'password', 'rtsp_url', 'substream_url']) delete payload[k]
        // A USB webcam picked (or switched) in the form; a browser webcam
        // stays one until a webcam is picked.
        if (form.source_device) payload.source_device = form.source_device
      }
      Object.keys(payload).forEach((k) => payload[k] === undefined && delete payload[k])
      const { data } = await apiService.updateCamera(editing.id, payload)
      setShowEditDialog(false)
      setEditing(null)
      resetForm()
      await refreshCameras()
      // A changed stream source is pushed to the media server before the edit
      // is committed, so reaching here means it landed. stream_warning only
      // ever carries a best-effort pause/resume hiccup — the row is saved
      // either way. A failed re-point never gets here: it throws 409/502 and
      // nothing was written.
      if (data?.stream_warning) {
        showWarning(`Camera saved, but ${data.stream_warning}`)
      } else if (data?.stream_action && data.stream_action !== 'none') {
        showSuccess('Camera updated — stream re-provisioned')
      } else {
        showSuccess('Camera updated')
      }
    } catch (e: any) {
      showError(extractApiError(e, 'Failed to update camera'))
    } finally {
      setSaving(false)
    }
  }

  const onDelete = async (c: Camera) => {
    if (!(await confirm({
      title: t('camera.confirmDeleteTitle', { name: c.name }),
      message: t('camera.confirmDeleteMessage'),
      confirmLabel: t('camera.delete'),
      danger: true,
    }))) return
    try {
      setMutating(true)
      await apiService.deleteCamera(c.id)
      // A browser webcam this tab publishes: stop it and let the webcam go.
      stopSession(c.id)
      await refreshCameras()
      showSuccess('Camera deleted')
    } catch (e: any) {
      showError(extractApiError(e, 'Failed to delete camera'))
    } finally {
      setMutating(false)
    }
  }

  const onBulkDelete = async () => {
    const ids = Array.from(selected)
    if (!ids.length) return
    if (!(await confirm({
      title: t('camera.confirmBulkDeleteTitle', { count: ids.length }),
      message: t('camera.confirmBulkDeleteMessage'),
      confirmLabel: t('camera.delete'),
      danger: true,
    }))) return
    try {
      setMutating(true)
      // Per-camera failures used to be swallowed and the toast said
      // "completed" regardless; count them and say which way it went.
      let failed = 0
      for (const id of ids) {
        try { await apiService.deleteCamera(id); stopSession(id) } catch { failed++ }
      }
      await refreshCameras()
      clearSelection()
      if (failed) showError(t('camera.bulkPartial', { failed, total: ids.length }))
      else showSuccess('Bulk delete completed')
    } catch (e: any) {
      showError(extractApiError(e, 'Bulk delete failed'))
    } finally {
      setMutating(false)
    }
  }

  const onResumeRecording = async (c: Camera) => {
    try {
      setMutating(true)
      await apiService.setCameraRecording(c.id, { enabled: true, reason: 'resumed from Cameras' })
      await refreshCameras()
      showSuccess(t('recordingPause.resumedOk', { name: c.name }))
    } catch (e: any) {
      showError(extractApiError(e, t('recordingPause.failedResume')))
    } finally {
      setMutating(false)
    }
  }

  // The ticked cameras, as far as this page knows them: a selection can
  // outlive a page change, so an id not on screen still goes through.
  const selectedCameras = (): Camera[] => {
    const byId = new Map([...(allPages ?? []), ...cameras].map((c) => [c.id, c]))
    return Array.from(selected).map((id) => byId.get(id) ?? ({ id, name: `#${id}`, is_active: true } as Camera))
  }

  /** One call per camera, failures counted rather than swallowed. Cameras the
   *  action does not apply to (`skip`) are left out and reported. */
  const runBulk = async (
    apply: (c: Camera) => Promise<unknown>,
    done: string,
    skip?: (c: Camera) => boolean,
  ) => {
    const all = selectedCameras()
    const targets = skip ? all.filter((c) => !skip(c)) : all
    const skipped = all.length - targets.length
    if (!targets.length) { showInfo(t('cameraBulk.nothingToDo')); return }
    try {
      setMutating(true)
      let failed = 0
      for (const c of targets) {
        try { await apply(c) } catch { failed++ }
      }
      await refreshCameras()
      clearSelection()
      if (failed) showError(t('camera.bulkPartial', { failed, total: targets.length }))
      else showSuccess(skipped ? `${done} ${t('cameraBulk.skipped', { count: skipped })}` : done)
    } finally {
      setMutating(false)
    }
  }

  const onBulkAction = async (action: BulkAction) => {
    const count = selected.size
    switch (action) {
      case 'assign':
        setShowBulkLocation(false)
        setShowBulkAssign(true)
        return
      case 'delete':
        return onBulkDelete()
      case 'location':
        setShowBulkAssign(false)
        setBulkLocation('')
        setShowBulkLocation(true)
        return
      case 'detection-on':
      case 'detection-off': {
        const on = action === 'detection-on'
        return runBulk(
          (c) => apiService.updateCamera(c.id, { detection_enabled: on }),
          t(on ? 'cameraBulk.detectionOnDone' : 'cameraBulk.detectionOffDone', { count }),
          (c) => (c.detection_enabled !== false) === on,
        )
      }
      case 'activate':
        return runBulk(
          (c) => apiService.updateCamera(c.id, { is_active: true }),
          t('cameraBulk.activateDone', { count }),
          (c) => c.is_active,
        )
      case 'deactivate':
        if (!(await confirm({
          title: t('cameraBulk.confirmDeactivateTitle', { count }),
          message: t('cameraBulk.confirmDeactivateMessage'),
          confirmLabel: t('cameraBulk.deactivate'),
          danger: true,
        }))) return
        return runBulk(
          (c) => apiService.updateCamera(c.id, { is_active: false }),
          t('cameraBulk.deactivateDone', { count }),
          (c) => !c.is_active,
        )
      case 'pause': {
        // A turned-off camera is not recording, so there is nothing to pause.
        const targets = selectedCameras().filter((c) => c.is_active && !c.recording_pause)
        if (!targets.length) { showInfo(t('cameraBulk.nothingToDo')); return }
        setPausing(targets)
        return
      }
      case 'resume':
        return runBulk(
          (c) => apiService.setCameraRecording(c.id, { enabled: true, reason: 'resumed from Cameras (bulk)' }),
          t('cameraBulk.resumeDone', { count }),
          (c) => !c.recording_pause,
        )
    }
  }

  const onBulkLocation = async (clear = false) => {
    const value = clear ? null : bulkLocation.trim()
    if (!clear && !value) return
    await runBulk(
      (c) => apiService.updateCamera(c.id, { location: value }),
      clear ? t('cameraBulk.locationCleared', { count: selected.size })
        : t('cameraBulk.locationDone', { count: selected.size, location: value ?? '' }),
    )
    queryClient.invalidateQueries({ queryKey: ['camera-locations'] })
    setShowBulkLocation(false)
  }

  const onBulkAssign = async () => {
    const ids = Array.from(selected)
    if (!ids.length || bulkUserId === '') return
    try {
      setMutating(true)
      let failed = 0
      for (const id of ids) {
        try {
          await apiService.assignCameraPermission(id, { user_id: Number(bulkUserId), can_view: bulkCanView, can_manage: bulkCanManage })
        } catch { failed++ }
      }
      setShowBulkAssign(false)
      clearSelection()
      if (failed) showError(t('camera.bulkPartial', { failed, total: ids.length }))
      else showSuccess('Bulk assign completed')
    } catch (e: any) {
      showError(extractApiError(e, 'Bulk assign failed'))
    } finally {
      setMutating(false)
    }
  }

  const startEdit = (c: Camera) => {
    setEditing(c)
    setShowCreateDialog(false)
    setForm({
      name: c.name,
      description: c.description || '',
      ip_address: c.ip_address,
      // Seed from the URL when it has a port: the server derives the column
      // from the URL on save regardless, so this is what will be stored — the
      // box may as well show it rather than a value about to be overwritten.
      port: rtspPortFromUrl(c.rtsp_url) ?? c.port,
      username: c.username || '',
      password: '',
      rtsp_url: c.rtsp_url || '',
      substream_url: c.substream_url || '',
      display_aspect_choice: aspectChoiceOf(c.display_aspect_ratio),
      display_aspect_custom: isCustomAspect(c.display_aspect_ratio)
        ? (c.display_aspect_ratio as string)
        : '',
      location: c.location || '',
      vlan: c.vlan || '',
      status: c.status || 'unknown',
      is_active: c.is_active,
      detection_enabled: c.detection_enabled !== false,
      source_device: c.source_device || '',
    })
    setShowEditDialog(true)
  }

  const closeEditDialog = () => {
    setShowEditDialog(false)
    setEditing(null)
    resetForm()
  }

  const hasNext = cameras.length === limit

  return (
    // The page is the window's height, as Search is: the table takes what
    // the filters leave and scrolls under its own header, and the pager
    // stays in view below it instead of a long page scrolling away.
    <section className="flex h-[calc(100vh-5rem)] min-h-[480px] flex-col gap-2">
      {/* Title and filters in one panel, as on Search: the title is the
          panel's header, the filters its body. The title bar, body padding
          and field height are Search's own, shared, so the two pages cannot
          drift apart. */}
      <div className="shrink-0 border border-[var(--border)] bg-[var(--panel-2)]">
        <PageTitleBar
          icon={<CameraIcon size={16} />}
          title={t('nav.cameras')}
          description={t('camera.description')}
          actions={(!liveUpdates || canManageCameras) ? (
            <>
              {/* Say so when pushed updates have stopped. The list still
                  refreshes on its own timer, so the badges are not frozen —
                  but they are no longer near-instant, and a status page that
                  hides that is worse than one that admits it. */}
              {!liveUpdates && (
                <span
                  className="inline-flex items-center gap-1.5 text-[11px] text-[var(--warn)]"
                  title="Reconnecting to the live event stream; status may lag by up to a minute"
                >
                  <span className="h-1.5 w-1.5 rounded-full bg-[var(--warn)] animate-pulse" aria-hidden="true" />
                  {t('dashboard.reconnecting')}
                </span>
              )}
              {canManageCameras && (
                <>
                  <button type="button" onClick={() => setShowImport(true)} className={TITLE_ACTION_CLASS} data-testid="cameras-import">
                    <Upload size={13} aria-hidden="true" />
                    {t('cameraIo.import')}
                  </button>
                  <button type="button" onClick={() => setShowExport(true)} className={TITLE_ACTION_CLASS} data-testid="cameras-export">
                    <Download size={13} aria-hidden="true" />
                    {t('cameraIo.export')}
                  </button>
                  <button
                    type="button"
                    onClick={() => { setShowCreateDialog(true); setEditing(null); resetForm() }}
                    className={TITLE_ACTION_CLASS}
                  >
                    <Plus size={13} aria-hidden="true" />
                    {t('camera.add')}
                  </button>
                </>
              )}
            </>
          ) : undefined}
        />

        {/* Filters. Bulk actions appear here only with a selection, so the
            row stays quiet in the common case. */}
        <div className={`flex flex-wrap items-center gap-3 ${TITLE_PANEL_BODY_CLASS}`}>
          <div className="relative w-64 max-w-full">
            <SearchIcon
              size={15}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[var(--text-dim)]"
              aria-hidden="true"
            />
            <input
              value={query}
              onChange={(e) => { setPage(1); setQuery(e.target.value) }}
              placeholder={t('camera.search')}
              aria-label={t('camera.search')}
              className="h-9 w-full border border-[var(--border)] bg-[var(--bg-2)] pl-9 pr-2 text-[13px] outline-none transition-colors placeholder:text-[color-mix(in_oklab,var(--text-dim)_50%,transparent)] focus:border-[var(--accent)] focus:placeholder:text-[color-mix(in_oklab,var(--text-dim)_20%,transparent)]"
            />
          </div>
          {/* Only once there is a location to pick: on a recorder where
              no camera has one, the menu would offer nothing. */}
          {(locations?.locations.length ?? 0) > 0 && (
            <select
              value={location}
              onChange={(e) => { setPage(1); setLocation(e.target.value) }}
              aria-label={t('camera.location')}
              title={t('camera.location')}
              className="h-9 w-52 max-w-full border border-[var(--border)] bg-[var(--bg-2)] pl-3 text-[13px] outline-none transition-colors focus:border-[var(--accent)] truncate"
            >
              <option value="">{t('camera.allLocations')}</option>
              {locations!.locations.map((l) => (
                <option key={l.location} value={l.location}>{l.location} ({l.count})</option>
              ))}
              {locations!.none > 0 && <option value={NO_LOCATION}>{t('camera.noLocation')} ({locations!.none})</option>}
            </select>
          )}
          {/* All / Active / Inactive, each with its count under the current
              search and location. */}
          <div role="radiogroup" aria-label={t('cameraBulk.status')} className="inline-flex h-9 border border-[var(--border)] bg-[var(--bg-2)]">
            {(['all', 'active', 'inactive'] as const).map((s) => (
              <button
                key={s}
                type="button"
                role="radio"
                aria-checked={status === s}
                onClick={() => pickStatus(s)}
                data-testid={`camera-status-${s}`}
                className={`px-3 text-[13px] inline-flex items-center gap-1.5 border-l first:border-l-0 border-[var(--border)] transition-colors ${
                  status === s ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-dim)] hover:text-[var(--text)]'
                }`}
              >
                {t(`cameraBulk.status_${s}`)}
                {counts && (
                  <span className={`font-mono tabular-nums text-[11px] ${status === s ? 'text-white/80' : 'text-[var(--text-dim)]'}`}>
                    {counts[s]}
                  </span>
                )}
              </button>
            ))}
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {/* Table or grid of thumbnails. */}
            <div role="radiogroup" aria-label={t('cameraBulk.view')} className="inline-flex h-9 border border-[var(--border)] bg-[var(--bg-2)]">
              {([['table', List], ['grid', LayoutGrid]] as const).map(([v, Icon]) => (
                <button
                  key={v}
                  type="button"
                  role="radio"
                  aria-checked={view === v}
                  onClick={() => pickView(v)}
                  title={t(`cameraBulk.view_${v}`)}
                  aria-label={t(`cameraBulk.view_${v}`)}
                  data-testid={`camera-view-${v}`}
                  className={`w-9 inline-flex items-center justify-center border-l first:border-l-0 border-[var(--border)] transition-colors ${
                    view === v ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-dim)] hover:text-[var(--text)]'
                  }`}
                >
                  <Icon size={15} aria-hidden="true" />
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      {canManageCameras && showImport && (
        <CameraImportDialog onClose={() => setShowImport(false)} onImported={() => { void refreshCameras() }} />
      )}
      {canManageCameras && showExport && (
        <CameraExportDialog
          onClose={() => setShowExport(false)}
          filters={{
            active_only: activeOnly,
            ...(debouncedQuery ? { q: debouncedQuery } : {}),
            ...(location === NO_LOCATION ? { no_location: true } : location ? { location } : {}),
          }}
        />
      )}

      {/* Create Camera Dialog — shared with Live View (discover / manual) */}
      {canManageCameras && showCreateDialog && (
        <AddCameraDialog
          title="Add New Camera"
          onClose={() => { setShowCreateDialog(false); resetForm() }}
          onCameraAdded={async () => { setShowCreateDialog(false); resetForm(); await refreshCameras() }}
        />
      )}

      <div className="flex min-h-0 flex-col">
        {!camsQuery.isPending && cameras.length > 0 && renderSelectionBar()}
        {/* Bulk location and assign: popups, so the list never moves. */}
        {canManageCameras && showBulkLocation && selected.size > 0 && (
          <Modal
            open
            title={t('cameraBulk.locationTitle', { count: selected.size })}
            onClose={() => setShowBulkLocation(false)}
            widthClassName="w-[440px]"
            data-testid="camera-bulk-location-dialog"
          >
            <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); void onBulkLocation() }}>
              <Field label={t('camera.location')} hint={t('cameraBulk.locationHint')}>
                {(id) => (
                  <Input
                    id={id}
                    autoFocus
                    list="camera-bulk-locations"
                    value={bulkLocation}
                    maxLength={200}
                    onChange={(e) => setBulkLocation(e.target.value)}
                    placeholder={t('cameraBulk.locationPlaceholder')}
                    data-testid="camera-bulk-location-input"
                  />
                )}
              </Field>
              <datalist id="camera-bulk-locations">
                {(locations?.locations ?? []).map((l) => <option key={l.location} value={l.location} />)}
              </datalist>
              <div className="flex items-center gap-2">
                {/* Away from Apply, and confirmed: it wipes every selected
                    camera's location at once. */}
                <Button
                  variant="ghost"
                  className="mr-auto !text-[var(--danger)]"
                  onClick={async () => {
                    if (await confirm({
                      title: t('cameraBulk.confirmClearLocationTitle', { count: selected.size }),
                      confirmLabel: t('cameraBulk.clearLocation'),
                      danger: true,
                    })) void onBulkLocation(true)
                  }}
                  disabled={mutating}
                >
                  {t('cameraBulk.clearLocation')}
                </Button>
                <Button variant="ghost" onClick={() => setShowBulkLocation(false)}>{t('common.cancel')}</Button>
                <Button
                  type="submit"
                  variant="primary"
                  disabled={mutating || !bulkLocation.trim()}
                  data-testid="camera-bulk-location-apply"
                >
                  {t('cameraBulk.applyTo', { count: selected.size })}
                </Button>
              </div>
            </form>
          </Modal>
        )}
        {canManageCameras && showBulkAssign && selected.size > 0 && (
          <Modal
            open
            title={t('cameraBulk.assignTitle', { count: selected.size })}
            onClose={() => setShowBulkAssign(false)}
            widthClassName="w-[440px]"
            data-testid="camera-bulk-assign-dialog"
          >
            <div className="space-y-4 text-[13px]">
              <Field label={t('cameraBulk.assignUser')}>
                {(id) => (
                  <div className="relative">
                    <Input
                      id={id}
                      autoFocus
                      placeholder="Type username or email"
                      value={userQuery}
                      onChange={(e) => { setUserQuery(e.target.value); setBulkUserId('') }}
                    />
                    {userQuery && bulkUserId === '' && (
                      <div className="absolute z-10 mt-1 w-full bg-[var(--panel)] border border-[var(--border)] max-h-56 overflow-auto">
                        {usersLoading ? (
                          <div className="px-2 py-1 text-[var(--text-dim)]">Searching…</div>
                        ) : userOptions.length === 0 ? (
                          <div className="px-2 py-1 text-[var(--text-dim)]">No users</div>
                        ) : (
                          userOptions.map(u => (
                            <button
                              type="button"
                              key={u.id}
                              className="block w-full text-left px-2 py-1 hover:bg-[var(--panel-2)]"
                              onClick={() => { setBulkUserId(u.id); setUserQuery(u.username) }}
                            >
                              <span className="text-[var(--text)]">{u.username}</span>
                              <span className="text-[var(--text-dim)]"> · {u.email}</span>
                            </button>
                          ))
                        )}
                      </div>
                    )}
                  </div>
                )}
              </Field>
              <div className="flex items-center gap-4">
                <Checkbox className="!items-center !text-[13px]" label={t('camera.canView')} checked={bulkCanView} onChange={(e) => setBulkCanView(e.target.checked)} />
                <Checkbox className="!items-center !text-[13px]" label={t('camera.canManage')} checked={bulkCanManage} onChange={(e) => setBulkCanManage(e.target.checked)} />
              </div>
              <div className="flex justify-end gap-2">
                <Button variant="ghost" onClick={() => setShowBulkAssign(false)}>{t('common.cancel')}</Button>
                <Button
                  variant="primary"
                  onClick={onBulkAssign}
                  disabled={mutating || bulkUserId === ''}
                  title={bulkUserId === '' ? 'Pick a user first' : undefined}
                >
                  {t('cameraBulk.applyTo', { count: selected.size })}
                </Button>
              </div>
            </div>
          </Modal>
        )}
      {/* Cameras table. table-fixed with explicit widths is what keeps this
          from ever scrolling sideways: the fixed columns come to ~560px and
          Camera absorbs the rest, so content length no longer drives layout.
          Anything too long truncates and reveals in full via title. */}
      {/* Skeletons only on a cold load — `isPending` is true only when there
          is nothing cached to show. Every later fetch (a page change, the
          debounced re-search, the background refresh) keeps the previous rows
          on screen and just dims them, because swapping a populated table for
          skeletons flashes the whole page. */}
      {camsQuery.isPending ? (
        <div className="min-h-0 space-y-2 overflow-hidden">
          {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-10" />)}
        </div>
      ) : cameras.length === 0 ? (
        <EmptyState
          icon={<CameraOff size={28} />}
          title={t('camera.noCamerasTitle')}
          description={query || status !== 'all' || location
            ? t('camera.noMatch')
            : t('camera.addAction')}
          action={canManageCameras ? (
            <Button variant="primary" onClick={() => { setShowCreateDialog(true); setEditing(null); resetForm() }}>
              {t('camera.addAction')}
            </Button>
          ) : undefined}
        />
      ) : view === 'grid' ? (
        renderGrid()
      ) : (
        /* min-w is the floor, not the target. The fixed columns take ~670px,
           so below roughly 760px of table area the Camera column would be
           squeezed to nothing; past that point letting the wrapper scroll is
           the lesser evil. At any normal window width the table fits and no
           scrollbar appears. */
        <Table
          wrapperClassName="min-h-0 overflow-auto"
          className={`table-fixed min-w-[1000px] ${camsQuery.isFetching || mutating ? 'opacity-60 transition-opacity' : 'transition-opacity'}`}
        >
          {/* Sticks while the rows scroll under it. */}
          <THead className="sticky top-0 z-10 shadow-[0_1px_0_var(--border)]">
            <TR>
              <TH className="w-[26px] !pr-0" aria-label={t('cameraBulk.selectPage')} />
              <TH className="text-xs">{t('nav.cameras')}</TH>
              <TH className="text-xs w-[150px]">{t('camera.location')}</TH>
              <TH className="text-xs w-[100px]">{t('cameraBulk.status')}</TH>
              <TH className="text-xs w-[170px]">{t('camera.address')}</TH>
              <TH className="text-xs w-[150px]">{t('camera.stream')}</TH>
              <TH className="text-xs w-[150px]">{t('camera.recording')}</TH>
              <TH className="text-xs w-[140px]">{t('camera.actions')}</TH>
            </TR>
          </THead>
          <TBody striped>
            {cameras.map((c) => {
              const stream = streamState(c)
              const rec = recordingState(c)
              // Model and serial lost their own columns; keep every field
              // reachable from the hover text rather than dropping it.
              const device = [c.manufacturer, c.model].filter(Boolean).join(' ')
              const deviceLine = [device, c.firmware_version].filter(Boolean).join(' · ')
              const deviceTitle = [c.manufacturer, c.model, c.serial_number, c.firmware_version]
                .filter(Boolean).join(' · ')
              return (
                <TR key={c.id} className={c.is_active ? undefined : 'opacity-60'}>
                  <TD className="!pr-0">
                    <input
                      type="checkbox"
                      className="accent-[var(--accent)]"
                      checked={selected.has(c.id)}
                      onChange={(e) => toggleSelected(c.id, e.target.checked)}
                    />
                  </TD>
                  <TD className="truncate">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="truncate font-medium" title={c.name}>{c.name}</span>
                      {c.is_active && c.detection_enabled === false && (
                        <Badge variant="warning" className="shrink-0" title={t('cameras.detectionOffHint')}>{t('cameras.detectionOff')}</Badge>
                      )}
                    </div>
                    {deviceLine && (
                      <div className="text-xs text-[var(--text-dim)] truncate" title={deviceTitle || undefined}>
                        {deviceLine}
                      </div>
                    )}
                  </TD>
                  <TD className="truncate" title={c.location || undefined}>
                    {c.location?.trim() ? c.location : <span className="text-[var(--text-dim)]">—</span>}
                  </TD>
                  <TD>{activeBadge(c)}</TD>
                  {c.source_type === 'publisher' ? (
                    c.source_device ? (() => {
                      const cam = hostAgent?.webcams.find((w) => w.id === c.source_device)
                      const st = hostAgent?.connected ? hostAgent.streams.find((s) => s.camera_id === c.id) : undefined
                      const label = hostAgent && !hostAgent.connected ? 'Host agent not running' : describeStream(st)
                      return (
                        <TD className="whitespace-nowrap truncate" title={`Webcam on the OpenNVR server${cam ? ` (${cam.name})` : ''}: ${label}`}>
                          <span className="inline-flex items-center gap-1 text-[var(--text-dim)]"><Webcam size={13} /> Server · {cam?.name ?? 'webcam'}</span>
                        </TD>
                      )
                    })() : (
                      <TD className="whitespace-nowrap truncate" title="A webcam published from a browser tab">
                        <span className="inline-flex items-center gap-1 text-[var(--text-dim)]"><Webcam size={13} /> Browser webcam</span>
                      </TD>
                    )
                  ) : (
                    <TD className="whitespace-nowrap truncate" title={`${c.ip_address}:${c.port}`}>
                      {c.ip_address}<span className="text-[var(--text-dim)]">:{c.port}</span>
                    </TD>
                  )}
                  <TD>
                    <Badge variant={stream.variant} title={stream.title}>
                      {stream.icon && <Unplug size={12} />}
                      {stream.label}
                    </Badge>
                  </TD>
                  <TD>
                    <Badge variant={rec.variant} title={rec.title}>{rec.label}</Badge>
                  </TD>
                  <TD>
                    <div className="flex items-center justify-end gap-1">{cameraActions(c)}</div>
                  </TD>
                </TR>
              )
            })}
          </TBody>
        </Table>
      )}
      </div>

      {/* The server filters by status before counting, so total is exact. */}
      {(page > 1 || hasNext) && (
      <div className="shrink-0">
      <Pagination
        page={page}
        pageSize={limit}
        total={total}
        hasNext={hasNext}
        rowCount={cameras.length}
        pageSizeOptions={[10, 20, 50]}
        onPageChange={setPage}
        onPageSizeChange={(n) => { setPage(1); setLimit(n) }}
        isFetching={camsQuery.isFetching}
        label={t('camera.camerasNoun')}
      />
      </div>
      )}

      {peek && (
        <LivePeek
          cameraId={peek.id}
          cam={peek}
          onClose={() => setPeek(null)}
          footer={
            <Button size="sm" variant="outline" className="h-7 shrink-0" onClick={() => navigate(`/live?camera=${peek.id}`)}>
              {t('cameraBulk.openLiveView')}
            </Button>
          }
        />
      )}

      {pausing && (
        <RecordingPauseDialog
          cameras={pausing}
          allowed={pauseAllowed}
          onClose={() => setPausing(null)}
          onDone={() => { clearSelection(); refreshCameras() }}
        />
      )}

      {/* Edit Camera Dialog */}
      {canManageCameras && showEditDialog && editing && (
        <Modal
          open={showEditDialog}
          title={`Edit camera — ${editing.name}`}
          onClose={closeEditDialog}
          widthClassName="w-[640px]"
        >
          <form onSubmit={onUpdate} className="space-y-4">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <Field label="Name">
                <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
              </Field>
              {editing.source_type !== 'publisher' && (<>
              <Field label="IP address">
                <Input value={form.ip_address} onChange={(e) => setForm({ ...form, ip_address: e.target.value })} onBlur={() => syncIdentity('ip_address')} required />
              </Field>
              {/* Editable again: a port typed here is no longer discarded — it
                  is written into the RTSP URL on blur, which is what MediaMTX
                  actually pulls. The URL still wins on save, so the two cannot
                  drift apart the way they used to. */}
              <Field label="Port">
                <Input
                  type="number"
                  value={form.port}
                  onChange={(e) => setForm({ ...form, port: Number(e.target.value) })}
                  onBlur={() => syncIdentity('port')}
                  min={1}
                  max={65535}
                  title="Kept in step with the port in the RTSP URL below"
                />
              </Field>
              <Field label="Username">
                <Input value={form.username || ''} onChange={(e) => setForm({ ...form, username: e.target.value })} onBlur={() => syncIdentity('username')} />
              </Field>
              <Field label="Password">
                <Input type="password" value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} onBlur={() => syncIdentity('password')} placeholder="Leave blank to keep existing" />
              </Field>
              </>)}
              <Field label="Location">
                <Input value={form.location || ''} onChange={(e) => setForm({ ...form, location: e.target.value })} />
              </Field>
              <Field label="VLAN">
                <Input value={form.vlan || ''} onChange={(e) => setForm({ ...form, vlan: e.target.value })} />
              </Field>
              <Field label="Description">
                <Input value={form.description || ''} onChange={(e) => setForm({ ...form, description: e.target.value })} />
              </Field>
            </div>

            {editing.source_type === 'publisher' ? (
              editing.source_device ? (
                <Field label="Webcam on the OpenNVR server">
                  <HostWebcamPicker
                    value={form.source_device || null}
                    ownCameraId={editing.id}
                    onChange={(id) => setForm({ ...form, source_device: id })}
                  />
                </Field>
              ) : (
                <div className="space-y-2">
                  <p className="text-xs text-[var(--text-dim)]">
                    A webcam has no address or stream URL: its video is published
                    from a browser tab (the Webcam button on its row). To feed it
                    from a USB webcam on the OpenNVR server instead, pick one:
                  </p>
                  <HostWebcamPicker
                    value={form.source_device || null}
                    ownCameraId={editing.id}
                    onChange={(id) => setForm({ ...form, source_device: id })}
                  />
                </div>
              )
            ) : (<>
            <Field label="RTSP URL">
              {(id, describedBy) => (
                <div className="flex gap-2">
                  <Input id={id} aria-describedby={describedBy} className="flex-1" value={form.rtsp_url || ''} onChange={(e) => setForm({ ...form, rtsp_url: e.target.value })} onBlur={() => syncIdentity('rtsp_url')} />
                  <Button size="sm" variant="outline" className="whitespace-nowrap" onClick={() => setScanQr(true)} title="Scan the QR from the OpenNVR Cam app">Scan QR</Button>
                </div>
              )}
            </Field>
            {/* A camera saved before the fields were kept in step can open with
                the two already disagreeing. Say so rather than picking a winner:
                which one is stale is not knowable from here — a camera can be
                streaming perfectly from the URL's host while the IP column holds
                the wrong address, and silently "fixing" that would kill it. */}
            {urlHostMismatch && (
              <p className="text-xs text-[var(--warn)] -mt-2">
                The IP address ({form.ip_address}) and the RTSP URL host ({urlHostMismatch}) disagree.
                Editing either field updates the other — change the one that is wrong.
              </p>
            )}
            <Field label="Substream URL">
              <Input value={form.substream_url || ''} onChange={(e) => setForm({ ...form, substream_url: e.target.value })} placeholder="Optional low-res feed for the camera agent's live view" />
            </Field>
            </>)}

            {/* Display only — it never re-encodes and never re-provisions the
                stream. Auto covers encoders that squash the picture and signal
                no aspect ratio (Dahua/CP Plus "1080N" = 960x1080 for a 16:9
                scene); Native is the escape hatch if detection guesses wrong. */}
            <Field label="Display aspect ratio">
              {(id) => (
              <div className="space-y-1">
                <Select
                  id={id}
                  value={form.display_aspect_choice || 'auto'}
                  onChange={(e) => setForm({ ...form, display_aspect_choice: e.target.value })}
                >
                  {ASPECT_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </Select>
                {form.display_aspect_choice === 'custom' && (
                  <Input
                    value={form.display_aspect_custom || ''}
                    onChange={(e) => setForm({ ...form, display_aspect_custom: e.target.value })}
                    placeholder="e.g. 16:9"
                  />
                )}
                <p className="text-xs text-[var(--muted)]">
                  How OpenNVR shows this camera. Display only — recordings are
                  stored exactly as the camera sends them.
                </p>
              </div>
              )}
            </Field>

            <Field label="Skills">
              {() => (
              <div className="space-y-2">
                {/* Skills follow apps: a camera carries exactly the model
                    skills the enabled apps using it bring, so there is
                    nothing to edit here — only who uses it, and what that
                    brings. To run a skill on this camera, select the camera
                    on the app's own page (Applications → the app → Configure). */}
                <p className="text-xs text-[var(--muted)]">
                  Skills come from apps. Select this camera in an app's
                  page (Applications → the app → Configure → Cameras) to run
                  that app's skills on it; disable the app to stop them.
                </p>
                <CameraUsedBy cameraId={editing.id} />
                {(editing.assignments || []).length > 0 && (
                  <p className="text-xs text-[var(--muted)]">
                    Currently: {(editing.assignments || []).map(a => a.skill).join(', ')}
                  </p>
                )}
              </div>
              )}
            </Field>

            <div className="flex flex-col gap-2">
              <Checkbox label="Active" checked={!!form.is_active} onChange={(e) => setForm({ ...form, is_active: e.target.checked })} />
              <Checkbox
                label={t('cameras.detectionEnabled')}
                hint={t('cameras.detectionOffHint')}
                checked={form.detection_enabled !== false}
                onChange={(e) => setForm({ ...form, detection_enabled: e.target.checked })}
              />
            </div>

            <div className="flex justify-end gap-2 border-t border-[var(--border)] pt-4">
              <Button variant="outline" onClick={closeEditDialog}>Cancel</Button>
              <Button type="submit" variant="primary" disabled={saving}>{saving ? 'Updating…' : 'Update camera'}</Button>
            </div>
          </form>

          {scanQr && (
            <QrScanner
              title="Scan the QR from the OpenNVR Cam app"
              onResult={(text) => {
                // Same unpacking as the add dialog: the scanned rtsp:// URL
                // carries host, port and credentials, so fill those fields
                // instead of leaving them for the operator to retype. The
                // name of a camera being edited is never overwritten.
                const scanned = parseCameraQr(text)
                setForm(f => ({
                  ...f,
                  ...scanned,
                  name: f.name.trim() || scanned.name || f.name,
                }))
                setScanQr(false)
              }}
              onClose={() => setScanQr(false)}
            />
          )}
        </Modal>
      )}
    </section>
  )
}


/** Read-only: the apps that picked this camera. Picks are made in each
 *  app's own configuration; this answers "what is this camera used for"
 *  from the camera's side. */
function CameraUsedBy({ cameraId }: { cameraId: number }) {
  const [apps, setApps] = useState<{ app_id: string; name: string }[] | null>(null)
  useEffect(() => {
    let alive = true
    apiService.getCameraUsedBy(cameraId)
      .then(({ data }) => { if (alive) setApps(data?.apps ?? []) })
      .catch(() => { if (alive) setApps(null) })
    return () => { alive = false }
  }, [cameraId])
  if (apps === null) return null
  return (
    <p className="text-xs text-[var(--muted)]">
      Used by:{' '}
      {apps.length === 0
        ? 'no app yet'
        : apps.map((a) => a.name).join(', ')}
    </p>
  )
}
