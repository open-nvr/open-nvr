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

import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { apiService } from '../lib/apiService'
import { queryClient, useCameras, useMediaMtxHealth } from '../lib/queries'
import { useCameraStatusConnected } from '../hooks/useCameraStatus'
import { extractApiError } from '../lib/apiError'
import { useSnackbar } from '../components/Snackbar'
import { usePermissions } from '../hooks/usePermissions'
import { CameraOff, Pencil, Trash2, Unplug, Video } from 'lucide-react'
import { Badge, Button, EmptyState, PageHeader, Skeleton, Table, THead, TBody, TR, TH, TD } from '../components/ui'
import type { BadgeVariant } from '../components/ui'
import { AddCameraDialog } from '../components/AddCameraDialog'
import { QrScanner } from '../components/QrScanner'
import { parseCameraQr } from '../lib/cameraQr'
import { ASPECT_OPTIONS, isCustomAspect } from '../lib/aspect'
import { parseRtspUrl, rtspPortFromUrl, syncCameraIdentity } from '../lib/cameraIdentity'
import type { IdentityField } from '../lib/cameraIdentity'
import { formatDuration } from '../lib/time'
import { Modal } from '../components/Modal'

// Per-camera capability assignment ("camera 1 does LPR") — slice 1 of
// docs/design/per-camera-assignment.md. Written only here (the camera
// settings surface); consumers read it from the internal endpoint.
type CameraAssignment = { skill: string; labels?: string[] | null }

// One row of GET /cameras/assignable-skills: a known skill with live
// availability. available === null means "couldn't tell" (KAI-C
// unreachable) and must never grey anything out.
type AssignableSkill = {
  skill: string
  label: string
  source: string
  available: boolean | null
  hint: string
}

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
  /** Display-only aspect override; null = auto-detect. See lib/aspect.ts. */
  display_aspect_ratio?: string | null
  location?: string | null
  vlan?: string | null
  status?: string | null
  owner_id: number
  is_active: boolean
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
  recording_state?: 'recording' | 'not_recording' | 'stalled' | 'never' | 'off' | null
  last_recording_at?: string | null
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

// Form-side assignment row: labels edited as a comma-separated string.
type AssignmentRow = { skill: string; labels: string }

type CameraForm = {
  name: string
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
  assignments: AssignmentRow[]
}

export function Cameras() {
  const navigate = useNavigate()
  const { hasPermission } = usePermissions()
  const canManageCameras = hasPermission('cameras.manage')
  const { showError, showSuccess, showInfo, showWarning } = useSnackbar()
  // A mutation (delete / bulk op) is in flight. Distinct from the list's own
  // fetching state: the list now refreshes on its own in the background, and
  // that must never disable the buttons.
  const [mutating, setMutating] = useState(false)
  const [activeOnly, setActiveOnly] = useState(true)
  const [limit, setLimit] = useState(20)
  const [page, setPage] = useState(1)
  const skip = useMemo(() => (page - 1) * limit, [page, limit])
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const liveUpdates = useCameraStatusConnected()

  // One request per pause in typing, not one per keystroke.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query), 250)
    return () => clearTimeout(t)
  }, [query])

  const camsQuery = useCameras({
    skip,
    limit,
    active_only: activeOnly,
    q: debouncedQuery || undefined,
  })
  const cameras = useMemo<Camera[]>(
    () => (camsQuery.data?.cameras ?? []) as Camera[],
    [camsQuery.data],
  )
  const total = camsQuery.data?.total ?? 0

  const mtxQuery = useMediaMtxHealth()
  // null while we genuinely don't know yet — the "Media Server is not running"
  // banner must not flash during the first round trip.
  const mediamtxAvailable = mtxQuery.isPending ? null : mtxQuery.data?.status === 'ok'

  useEffect(() => {
    if (camsQuery.isError) {
      showError(extractApiError(camsQuery.error, 'Failed to load cameras'))
    }
  }, [camsQuery.isError, camsQuery.error, showError])

  // A selection is scoped to what is currently listed, so changing the listing
  // drops it. Deliberately keyed on the view parameters and not on the data:
  // the list refetches itself every minute now, and wiping a half-made
  // selection on a background refresh would be maddening.
  useEffect(() => {
    setSelected(new Set())
  }, [skip, limit, activeOnly, debouncedQuery])

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

  // Known skills for the Assignments editor — fetched once per dialog
  // open; [] until loaded (the editor stays free-text either way).
  const [assignableSkills, setAssignableSkills] = useState<AssignableSkill[]>([])

  const loadAssignableSkills = async () => {
    try {
      const { data } = await apiService.getAssignableSkills()
      setAssignableSkills(data?.skills || [])
    } catch {
      setAssignableSkills([])
    }
  }

  const skillInfo = (raw: string): AssignableSkill | undefined => {
    const s = raw.trim().toLowerCase()
    return s ? assignableSkills.find(k => k.skill === s) : undefined
  }

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
    assignments: [],
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
    assignments: [],
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
    if (c.live_online === true) return { variant: 'success', label: 'Ready' }
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
    const seenAt = at ? `Last segment ${at.toLocaleString()}` : undefined

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
  const refreshCameras = () =>
    queryClient.invalidateQueries({ queryKey: ['cameras'] })

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
        // Full replace ([] clears): rows with an empty skill are dropped;
        // labels split on commas, blanks removed.
        assignments: form.assignments
          .map(r => ({
            skill: r.skill.trim().toLowerCase(),
            labels: r.labels.split(',').map(x => x.trim().toLowerCase()).filter(Boolean),
          }))
          .filter(r => r.skill)
          .map(r => (r.labels.length ? r : { skill: r.skill })),
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
      showError(e?.data?.detail || e?.message || 'Failed to update camera')
    } finally {
      setSaving(false)
    }
  }

  const onDelete = async (c: Camera) => {
    if (!confirm(
      `Delete camera "${c.name}"?\n\n` +
      'This stops its stream and recording immediately and moves it to ' +
      'Settings → Deleted Cameras. It cannot be edited or reactivated; its ' +
      'recordings stay viewable there until retention removes them.'
    )) return
    try {
      setMutating(true)
      await apiService.deleteCamera(c.id)
      await refreshCameras()
      showSuccess('Camera deleted')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to delete camera')
    } finally {
      setMutating(false)
    }
  }

  const onBulkDelete = async () => {
    const ids = Array.from(selected)
    if (!ids.length) return
    if (!confirm(
      `Delete ${ids.length} selected camera(s)?\n\n` +
      'This stops their streams and recording immediately and moves them to ' +
      'Settings → Deleted Cameras. They cannot be edited or reactivated; their ' +
      'recordings stay viewable there until retention removes them.'
    )) return
    try {
      setMutating(true)
      for (const id of ids) {
        try { await apiService.deleteCamera(id) } catch { }
      }
      await refreshCameras()
      setSelected(new Set())
      showSuccess('Bulk delete completed')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Bulk delete failed')
    } finally {
      setMutating(false)
    }
  }

  const onBulkAssign = async () => {
    const ids = Array.from(selected)
    if (!ids.length || bulkUserId === '') return
    try {
      setMutating(true)
      for (const id of ids) {
        try {
          await apiService.assignCameraPermission(id, { user_id: Number(bulkUserId), can_view: bulkCanView, can_manage: bulkCanManage })
        } catch { }
      }
      setShowBulkAssign(false)
      setSelected(new Set())
      showSuccess('Bulk assign completed')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Bulk assign failed')
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
      assignments: (c.assignments || []).map(a => ({
        skill: a.skill,
        labels: (a.labels || []).join(', '),
      })),
    })
    setShowEditDialog(true)
    loadAssignableSkills()
  }

  const closeEditDialog = () => {
    setShowEditDialog(false)
    setEditing(null)
    resetForm()
  }

  const totalPages = Math.max(1, Math.ceil(total / limit))
  const hasNext = cameras.length === limit

  return (
    <section className="space-y-4">
      {/* Header */}
      <PageHeader
        title="Cameras"
        description="Every camera registered with this recorder, with its live stream and recording state."
        actions={
          <div className="flex items-center gap-3">
            {/* Say so when pushed updates have stopped. The list still
                refreshes on its own timer, so the badges are not frozen —
                but they are no longer near-instant, and a status page that
                hides that is worse than one that admits it. */}
            {!liveUpdates && (
              <span className="text-xs text-[var(--text-dim)]" title="Reconnecting to the live event stream; status may lag by up to a minute">
                Live updates reconnecting…
              </span>
            )}
            {canManageCameras && (
              <Button variant="primary" onClick={() => { setShowCreateDialog(true); setEditing(null); resetForm() }}>
                Add Camera
              </Button>
            )}
          </div>
        }
      />

      {/* Filters. Bulk actions appear here only with a selection, so the row
          stays quiet in the common case. */}
      <div className="flex items-center gap-2 text-sm flex-wrap">
        <input
          className="bg-[var(--panel-2)] border border-[var(--border)] px-2 py-1 rounded"
          placeholder="Search name or IP"
          value={query}
          onChange={(e) => { setPage(1); setQuery(e.target.value) }}
        />
        <label className="inline-flex items-center gap-1">
          <input type="checkbox" className="accent-[var(--accent)]" checked={activeOnly} onChange={(e) => { setPage(1); setActiveOnly(e.target.checked) }} /> Active only
        </label>
        <select className="bg-[var(--panel-2)] border border-[var(--border)] px-2 py-1 rounded" value={limit} onChange={(e) => { setPage(1); setLimit(Number(e.target.value)) }}>
          {[10, 20, 50].map(n => <option key={n} value={n}>{n}/page</option>)}
        </select>
        {canManageCameras && selected.size > 0 && (
          <div className="flex items-center gap-2 ml-auto">
            <Button variant="danger" onClick={onBulkDelete} disabled={mutating}>
              Delete Selected ({selected.size})
            </Button>
            <Button variant="outline" onClick={() => setShowBulkAssign((s) => !s)} disabled={mutating}>
              Assign Permissions
            </Button>
          </div>
        )}
      </div>

      {/* Bulk Assign Panel */}
      {canManageCameras && showBulkAssign && selected.size > 0 && (
        <div className="border border-[var(--border)] bg-[var(--panel-2)] p-3 text-sm flex items-center gap-3 flex-wrap">
          <div className="text-[var(--text-dim)]">Assign to user</div>
          <div className="relative">
            <input
              className="bg-[var(--panel)] border border-[var(--border)] px-2 py-1 w-56"
              placeholder="Type username or email"
              value={userQuery}
              onChange={(e) => { setUserQuery(e.target.value); setBulkUserId('') }}
            />
            {userQuery && (
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
                      className={`block w-full text-left px-2 py-1 hover:bg-[var(--panel-2)] ${bulkUserId === u.id ? 'bg-[var(--panel-2)]' : ''}`}
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
          <label className="inline-flex items-center gap-2">
            <input type="checkbox" className="accent-[var(--accent)]" checked={bulkCanView} onChange={(e) => setBulkCanView(e.target.checked)} /> can_view
          </label>
          <label className="inline-flex items-center gap-2">
            <input type="checkbox" className="accent-[var(--accent)]" checked={bulkCanManage} onChange={(e) => setBulkCanManage(e.target.checked)} /> can_manage
          </label>
          <button className="px-3 py-1 bg-[var(--accent)] text-white" onClick={onBulkAssign} disabled={mutating || bulkUserId === ''}>Apply to {selected.size} selected</button>
          <button className="px-3 py-1 bg-[var(--panel)] border border-[var(--border)]" onClick={() => setShowBulkAssign(false)}>Cancel</button>
        </div>
      )}

      {/* Create Camera Dialog — shared with Live View (discover / manual) */}
      {canManageCameras && showCreateDialog && (
        <AddCameraDialog
          title="Add New Camera"
          onClose={() => { setShowCreateDialog(false); resetForm() }}
          onCameraAdded={async () => { setShowCreateDialog(false); resetForm(); await refreshCameras() }}
        />
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
        <div className="space-y-2">
          {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-10" />)}
        </div>
      ) : cameras.length === 0 ? (
        <EmptyState
          icon={<CameraOff size={28} />}
          title="No cameras"
          description={query || activeOnly
            ? 'No cameras match the current search or filter. Try clearing them.'
            : 'Add a camera to start recording.'}
          action={canManageCameras ? (
            <Button variant="primary" onClick={() => { setShowCreateDialog(true); setEditing(null); resetForm() }}>
              Add Camera
            </Button>
          ) : undefined}
        />
      ) : (
        /* min-w is the floor, not the target. The fixed columns take ~570px,
           so below roughly 760px of table area the Camera column would be
           squeezed to nothing; past that point letting the wrapper scroll is
           the lesser evil. At any normal window width the table fits and no
           scrollbar appears. */
        <Table className={`table-fixed min-w-[760px] ${camsQuery.isFetching || mutating ? 'opacity-60 transition-opacity' : 'transition-opacity'}`}>
          <THead>
            <TR>
              <TH className="w-10">
                <input
                  type="checkbox"
                  className="accent-[var(--accent)]"
                  checked={cameras.length > 0 && cameras.every(c => selected.has(c.id))}
                  onChange={(e) => {
                    if (e.target.checked) {
                      setSelected(new Set(cameras.map(c => c.id)))
                    } else {
                      setSelected(new Set())
                    }
                  }}
                />
              </TH>
              <TH>Camera</TH>
              <TH className="w-[170px]">Address</TH>
              <TH className="w-[150px]">Stream</TH>
              <TH className="w-[150px]">Recording</TH>
              <TH className="w-[110px]">Actions</TH>
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
                  <TD>
                    <input
                      type="checkbox"
                      className="accent-[var(--accent)]"
                      checked={selected.has(c.id)}
                      onChange={(e) => {
                        const next = new Set(selected)
                        if (e.target.checked) next.add(c.id); else next.delete(c.id)
                        setSelected(next)
                      }}
                    />
                  </TD>
                  <TD className="truncate">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="truncate font-medium" title={c.name}>{c.name}</span>
                      {/* The Active column is gone, so with "Active only"
                          unticked this badge is the only thing distinguishing
                          a deactivated camera. */}
                      {!c.is_active && <Badge variant="neutral" className="shrink-0">Inactive</Badge>}
                    </div>
                    <div className="text-xs text-[var(--text-dim)] truncate" title={deviceTitle || undefined}>
                      {deviceLine || '—'}
                    </div>
                  </TD>
                  <TD className="whitespace-nowrap truncate" title={`${c.ip_address}:${c.port}`}>
                    {c.ip_address}<span className="text-[var(--text-dim)]">:{c.port}</span>
                  </TD>
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
                    <div className="flex items-center justify-end gap-1">
                      {canManageCameras && (
                        <button className={ICON_BTN} onClick={() => startEdit(c)} title="Edit camera" aria-label={`Edit ${c.name}`}>
                          <Pencil size={15} />
                        </button>
                      )}
                      {/* Recording is automatic on an NVR — no manual
                          start/stop control. The Recording column shows its
                          live status, derived from the newest written
                          segment rather than the config flag. */}
                      {c.mediamtx_provisioned === true && (
                        <button
                          className={`${ICON_BTN} border-[var(--accent)]/50 text-[var(--accent)] hover:bg-[var(--accent)]/10`}
                          onClick={() => navigate(`/live?camera=${c.id}`)}
                          title="View live"
                          aria-label={`View ${c.name} live`}
                        >
                          <Video size={15} />
                        </button>
                      )}
                      {canManageCameras && (
                        <button
                          className={`${ICON_BTN} hover:border-red-600 hover:bg-red-900/30 hover:text-red-400`}
                          onClick={() => onDelete(c)}
                          title="Delete camera"
                          aria-label={`Delete ${c.name}`}
                        >
                          <Trash2 size={15} />
                        </button>
                      )}
                    </div>
                  </TD>
                </TR>
              )
            })}
          </TBody>
        </Table>
      )}

      {/* Pagination */}
      <div className="flex items-center gap-2 text-sm">
        <Button disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>Prev</Button>
        {/* activeOnly filters client-side of the count the backend returns, so
            totalPages would lie — fall back to the hasNext probe there. */}
        {activeOnly ? (
          <span>Page {page}</span>
        ) : (
          <span>Page {page} / {totalPages}</span>
        )}
        <Button disabled={activeOnly ? !hasNext : page >= totalPages} onClick={() => setPage((p) => p + 1)}>Next</Button>
      </div>

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
                <input className={EDIT_INPUT} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
              </Field>
              <Field label="IP address">
                <input className={EDIT_INPUT} value={form.ip_address} onChange={(e) => setForm({ ...form, ip_address: e.target.value })} onBlur={() => syncIdentity('ip_address')} required />
              </Field>
              {/* Editable again: a port typed here is no longer discarded — it
                  is written into the RTSP URL on blur, which is what MediaMTX
                  actually pulls. The URL still wins on save, so the two cannot
                  drift apart the way they used to. */}
              <Field label="Port">
                <input
                  type="number"
                  className={EDIT_INPUT}
                  value={form.port}
                  onChange={(e) => setForm({ ...form, port: Number(e.target.value) })}
                  onBlur={() => syncIdentity('port')}
                  min={1}
                  max={65535}
                  title="Kept in step with the port in the RTSP URL below"
                />
              </Field>
              <Field label="Username">
                <input className={EDIT_INPUT} value={form.username || ''} onChange={(e) => setForm({ ...form, username: e.target.value })} onBlur={() => syncIdentity('username')} />
              </Field>
              <Field label="Password">
                <input type="password" className={EDIT_INPUT} value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} onBlur={() => syncIdentity('password')} placeholder="Leave blank to keep existing" />
              </Field>
              <Field label="Location">
                <input className={EDIT_INPUT} value={form.location || ''} onChange={(e) => setForm({ ...form, location: e.target.value })} />
              </Field>
              <Field label="VLAN">
                <input className={EDIT_INPUT} value={form.vlan || ''} onChange={(e) => setForm({ ...form, vlan: e.target.value })} />
              </Field>
              <Field label="Description">
                <input className={EDIT_INPUT} value={form.description || ''} onChange={(e) => setForm({ ...form, description: e.target.value })} />
              </Field>
            </div>

            <Field label="RTSP URL">
              <div className="flex gap-2">
                <input className={`flex-1 ${EDIT_INPUT}`} value={form.rtsp_url || ''} onChange={(e) => setForm({ ...form, rtsp_url: e.target.value })} onBlur={() => syncIdentity('rtsp_url')} />
                <button type="button" className="px-3 py-2 border border-[var(--border)] bg-[var(--panel-2)] rounded text-xs whitespace-nowrap" onClick={() => setScanQr(true)} title="Scan the QR from the OpenNVR Cam app">Scan QR</button>
              </div>
            </Field>
            {/* A camera saved before the fields were kept in step can open with
                the two already disagreeing. Say so rather than picking a winner:
                which one is stale is not knowable from here — a camera can be
                streaming perfectly from the URL's host while the IP column holds
                the wrong address, and silently "fixing" that would kill it. */}
            {urlHostMismatch && (
              <p className="text-xs text-amber-400/90 -mt-2">
                The IP address ({form.ip_address}) and the RTSP URL host ({urlHostMismatch}) disagree.
                Editing either field updates the other — change the one that is wrong.
              </p>
            )}
            <Field label="Substream URL">
              <input className={EDIT_INPUT} value={form.substream_url || ''} onChange={(e) => setForm({ ...form, substream_url: e.target.value })} placeholder="Optional low-res feed for the camera agent's live view" />
            </Field>

            {/* Display only — it never re-encodes and never re-provisions the
                stream. Auto covers encoders that squash the picture and signal
                no aspect ratio (Dahua/CP Plus "1080N" = 960x1080 for a 16:9
                scene); Native is the escape hatch if detection guesses wrong. */}
            <Field label="Display aspect ratio">
              <div className="space-y-1">
                <select
                  className={EDIT_INPUT}
                  value={form.display_aspect_choice || 'auto'}
                  onChange={(e) => setForm({ ...form, display_aspect_choice: e.target.value })}
                >
                  {ASPECT_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
                {form.display_aspect_choice === 'custom' && (
                  <input
                    className={EDIT_INPUT}
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
            </Field>

            <Field label="Assignments">
              <div className="space-y-2">
                <p className="text-xs text-[var(--muted)]">
                  What this camera is <em>for</em> — e.g. skill{' '}
                  <code>license_plate_recognition</code>, or{' '}
                  <code>object_detection</code> narrowed to labels{' '}
                  <code>person, truck</code>. Nothing assigned = no
                  restriction declared. Consumers (Tier-0, apps) adopt these
                  incrementally.
                </p>
                <datalist id="assignable-skills">
                  {assignableSkills.map(k => (
                    <option key={k.skill} value={k.skill}>
                      {`${k.label}${k.available === false ? ' (not installed)' : ''}`}
                    </option>
                  ))}
                </datalist>
                {form.assignments.map((row, i) => (
                  <div key={i} className="flex gap-2 items-center">
                    <input
                      className={`flex-1 ${EDIT_INPUT}`}
                      value={row.skill}
                      onChange={(e) => setForm({
                        ...form,
                        assignments: form.assignments.map((r, j) => j === i ? { ...r, skill: e.target.value } : r),
                      })}
                      placeholder="skill, e.g. object_detection"
                      list="assignable-skills"
                      aria-label={`Assignment ${i + 1} skill`}
                    />
                    <input
                      className={`flex-1 ${EDIT_INPUT}`}
                      value={row.labels}
                      onChange={(e) => setForm({
                        ...form,
                        assignments: form.assignments.map((r, j) => j === i ? { ...r, labels: e.target.value } : r),
                      })}
                      placeholder="labels (optional), e.g. person, truck"
                      aria-label={`Assignment ${i + 1} labels`}
                    />
                    <button
                      type="button"
                      className="px-2 py-2 border border-[var(--border)] bg-[var(--panel-2)] rounded text-xs"
                      onClick={() => setForm({ ...form, assignments: form.assignments.filter((_, j) => j !== i) })}
                      title="Remove assignment"
                      aria-label={`Remove assignment ${i + 1}`}
                    >✕</button>
                  </div>
                ))}
                {form.assignments.map((row, i) => {
                  const info = skillInfo(row.skill)
                  if (!row.skill.trim()) return null
                  // Only ANNOTATE — never block: available === false gets an
                  // amber "what to install" note; null (unknown) and true
                  // stay quiet; an unknown spelling gets a gentle nudge.
                  if (info && info.available === false) {
                    return (
                      <p key={`hint-${i}`} className="text-xs text-amber-500">
                        {info.label}: {info.hint}
                      </p>
                    )
                  }
                  if (!info && assignableSkills.length > 0) {
                    return (
                      <p key={`hint-${i}`} className="text-xs text-[var(--muted)]">
                        “{row.skill.trim()}” isn’t a known skill on this install —
                        it will be stored, but nothing consumes it yet.
                      </p>
                    )
                  }
                  return null
                })}
                <button
                  type="button"
                  className="px-3 py-1.5 border border-[var(--border)] bg-[var(--panel-2)] rounded text-xs"
                  onClick={() => setForm({ ...form, assignments: [...form.assignments, { skill: '', labels: '' }] })}
                  disabled={form.assignments.length >= 8}
                >+ Add assignment</button>
              </div>
            </Field>

            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" className="accent-[var(--accent)]" checked={!!form.is_active} onChange={(e) => setForm({ ...form, is_active: e.target.checked })} /> Active
            </label>

            <div className="flex justify-end gap-2 border-t border-[var(--border)] pt-4">
              <button type="button" className="px-4 py-2 border border-[var(--border)] bg-[var(--panel-2)] rounded" onClick={closeEditDialog}>Cancel</button>
              <button type="submit" className="px-4 py-2 bg-[var(--accent)] text-white rounded disabled:opacity-60" disabled={saving}>{saving ? 'Updating…' : 'Update camera'}</button>
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

const EDIT_INPUT =
  'bg-[var(--panel-2)] border border-[var(--border)] px-3 py-2 rounded text-sm'

// A plain button rather than the Button primitive, deliberately. Button bakes
// in px-3 py-1.5, and clsx is not tailwind-merge — both classes reach the DOM
// and Tailwind emits utilities in ascending order, so .px-3 lands after
// .px-1.5 and wins at equal specificity. A className can therefore only make
// a Button bigger, never smaller, and three full-size buttons overflow this
// column.
const ICON_BTN =
  'inline-flex items-center justify-center rounded border border-[var(--border)] bg-[var(--panel-2)] p-1.5 text-[var(--text-dim)] transition-colors hover:bg-[var(--panel)] hover:text-[var(--text)]'

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[var(--text-dim)]">{label}</span>
      {children}
    </label>
  )
}
