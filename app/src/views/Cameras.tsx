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

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { apiService } from '../lib/apiService'
import { useAuth } from '../auth/AuthContext'
import { api } from '../lib/api'
import { useSnackbar } from '../components/Snackbar'
import { usePermissions } from '../hooks/usePermissions'
import { CameraOff, Pencil, Trash2, Unplug, Video } from 'lucide-react'
import { Badge, Button, EmptyState, PageHeader, Skeleton, Table, THead, TBody, TR, TH, TD } from '../components/ui'
import type { BadgeVariant } from '../components/ui'
import { AddCameraDialog } from '../components/AddCameraDialog'
import { QrScanner } from '../components/QrScanner'
import { parseCameraQr } from '../lib/cameraQr'
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
  location?: string | null
  vlan?: string | null
  status?: string | null
  owner_id: number
  is_active: boolean
  deleted_at?: string | null
  mediamtx_provisioned?: boolean | null
  // Configuration intent — always true for a provisioned camera and not
  // switchable, so it says nothing about whether footage is being written.
  recording_enabled?: boolean | null
  // Observed recording health, derived server-side from the newest indexed
  // segment. Absent (undefined) means the endpoint didn't compute it.
  recording_state?: 'recording' | 'stalled' | 'never' | 'off' | null
  last_recording_at?: string | null
  // ONVIF device metadata
  manufacturer?: string | null
  model?: string | null
  firmware_version?: string | null
  serial_number?: string | null
  hardware_id?: string | null
  assignments?: CameraAssignment[] | null
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
  location?: string
  vlan?: string
  status?: string
  is_active?: boolean
  assignments: AssignmentRow[]
}

export function Cameras() {
  const navigate = useNavigate()
  const { token, loading: authLoading } = useAuth()
  const { hasPermission } = usePermissions()
  const canManageCameras = hasPermission('cameras.manage')
  const { showError, showSuccess, showInfo } = useSnackbar()
  const [loading, setLoading] = useState(false)
  const [cameras, setCameras] = useState<Camera[]>([])
  const [total, setTotal] = useState(0)
  const [activeOnly, setActiveOnly] = useState(true)
  const [limit, setLimit] = useState(20)
  const [page, setPage] = useState(1)
  const skip = useMemo(() => (page - 1) * limit, [page, limit])
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [mediamtxAvailable, setMediamtxAvailable] = useState<boolean | null>(null)
  const [streamStatuses, setStreamStatuses] = useState<Record<number, { ready: boolean; bytesReceived: number }>>({})

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
  // Dedicated to the edit form. Previously the Update button was bound to the
  // shared `loading` flag, which the camera-list effect also sets — a refresh
  // in flight left the button disabled and looking broken.
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
    location: '',
    vlan: '',
    status: 'unknown',
    assignments: [],
  })

  // Fetch streaming status for cameras
  const fetchStreamStatuses = useCallback(async (cameraList: Camera[]) => {
    const results: Record<number, { ready: boolean; bytesReceived: number }> = {}
    await Promise.all(
      cameraList.map(async (c) => {
        try {
          const { data } = await apiService.getCameraMediaMTXStatus(c.id)
          const details = data?.active_path?.details
          results[c.id] = {
            ready: details?.ready === true,
            bytesReceived: details?.bytesReceived || 0
          }
        } catch {
          results[c.id] = { ready: false, bytesReceived: 0 }
        }
      })
    )
    setStreamStatuses(results)
  }, [])

  // The five stream states, unchanged from the hand-rolled badges this
  // replaced — only the styling moved onto the shared Badge, whose colours
  // come from the --badge-* tokens and so survive the light theme.
  const streamState = (c: Camera): { variant: BadgeVariant; label: string; title?: string; icon?: boolean } => {
    if (mediamtxAvailable === false) {
      return { variant: 'warning', label: 'Disconnected', title: 'Media Server is not running', icon: true }
    }
    const status = streamStatuses[c.id]
    if (status?.ready && status?.bytesReceived > 0) return { variant: 'success', label: 'Ready' }
    if (c.mediamtx_provisioned === true) {
      return { variant: 'warning', label: 'Disconnected', title: 'Provisioned but not streaming' }
    }
    if (c.mediamtx_provisioned === false) return { variant: 'destructive', label: 'Error' }
    return { variant: 'neutral', label: 'Not configured' }
  }

  // Observed recording health, not the config flag. `recording_enabled` is
  // true for every provisioned camera and cannot be switched off, so it used
  // to claim "Recording" beside a dead stream. The server derives this from
  // the newest written segment instead, using the recording watchdog's own
  // thresholds, so this badge agrees with the stall alert.
  const recordingState = (c: Camera): { variant: BadgeVariant; label: string; title?: string } => {
    const at = c.last_recording_at ? new Date(c.last_recording_at) : null
    const agoSeconds = at ? Math.max(0, (Date.now() - at.getTime()) / 1000) : null
    const seenAt = at ? `Last segment ${at.toLocaleString()}` : undefined

    switch (c.recording_state) {
      case 'recording':
        return { variant: 'success', label: 'Recording', title: seenAt }
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

  // Load cameras
  useEffect(() => {
    if (authLoading) return
    let alive = true
    const run = async () => {
      try {
        setLoading(true)
        if (token) api.setToken(token)
        
        // Check MediaMTX availability
        let mtxHealthy = false
        try {
          const { data: healthData } = await apiService.mtxHealth()
          mtxHealthy = healthData?.status === 'ok'
          if (alive) setMediamtxAvailable(mtxHealthy)
        } catch {
          if (alive) setMediamtxAvailable(false)
        }
        
        const { data } = await apiService.getCameras({ skip, limit, active_only: activeOnly, q: query || undefined })
        if (alive) {
          setCameras(data.cameras)
          setTotal(data.total ?? 0)
          setSelected(new Set())
          
          // Fetch streaming status for each camera if MediaMTX is available
          if (mtxHealthy && data.cameras?.length) {
            fetchStreamStatuses(data.cameras)
          }
        }
      } catch (e: any) {
        if (alive) showError(e?.data?.detail || e?.message || 'Failed to load cameras')
      } finally {
        if (alive) setLoading(false)
      }
    }
    // Debounced so typing in the search box costs one request after a pause
    // rather than one per keystroke — the same two lines the user-search
    // effect below already uses.
    const t = setTimeout(run, 250)
    return () => { alive = false; clearTimeout(t) }
  }, [token, authLoading, skip, limit, activeOnly, query, fetchStreamStatuses])

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

  const refreshCameras = async () => {
    const { data } = await apiService.getCameras({ skip, limit, active_only: activeOnly, q: query || undefined })
    setCameras(data.cameras)
    setTotal(data.total ?? 0)
    // Refresh streaming status if MediaMTX is available
    if (mediamtxAvailable && data.cameras?.length) {
      fetchStreamStatuses(data.cameras)
    }
  }

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
      await apiService.updateCamera(editing.id, payload)
      setShowEditDialog(false)
      setEditing(null)
      resetForm()
      await refreshCameras()
      showSuccess('Camera updated')
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
      setLoading(true)
      await apiService.deleteCamera(c.id)
      await refreshCameras()
      showSuccess('Camera deleted')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to delete camera')
    } finally {
      setLoading(false)
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
      setLoading(true)
      for (const id of ids) {
        try { await apiService.deleteCamera(id) } catch { }
      }
      await refreshCameras()
      setSelected(new Set())
      showSuccess('Bulk delete completed')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Bulk delete failed')
    } finally {
      setLoading(false)
    }
  }

  const onBulkAssign = async () => {
    const ids = Array.from(selected)
    if (!ids.length || bulkUserId === '') return
    try {
      setLoading(true)
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
      setLoading(false)
    }
  }

  const startEdit = (c: Camera) => {
    setEditing(c)
    setShowCreateDialog(false)
    setForm({
      name: c.name,
      description: c.description || '',
      ip_address: c.ip_address,
      port: c.port,
      username: c.username || '',
      password: '',
      rtsp_url: c.rtsp_url || '',
      substream_url: c.substream_url || '',
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
        actions={canManageCameras ? (
          <Button variant="primary" onClick={() => { setShowCreateDialog(true); setEditing(null); resetForm() }}>
            Add Camera
          </Button>
        ) : undefined}
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
            <Button variant="danger" onClick={onBulkDelete} disabled={loading}>
              Delete Selected ({selected.size})
            </Button>
            <Button variant="outline" onClick={() => setShowBulkAssign((s) => !s)} disabled={loading}>
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
          <button className="px-3 py-1 bg-[var(--accent)] text-white" onClick={onBulkAssign} disabled={loading || bulkUserId === ''}>Apply to {selected.size} selected</button>
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
      {/* Skeletons only on a cold load. `loading` is also set by delete and
          the debounced re-search, and swapping a populated table for
          skeletons on every one of those flashes the whole page — so once
          there are rows to show, a later load just dims them instead. */}
      {loading && cameras.length === 0 ? (
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
        <Table className={`table-fixed min-w-[760px] ${loading ? 'opacity-60 transition-opacity' : 'transition-opacity'}`}>
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
                <input className={EDIT_INPUT} value={form.ip_address} onChange={(e) => setForm({ ...form, ip_address: e.target.value })} required />
              </Field>
              <Field label="Port">
                <input type="number" className={EDIT_INPUT} value={form.port} onChange={(e) => setForm({ ...form, port: Number(e.target.value) })} min={1} max={65535} />
              </Field>
              <Field label="Username">
                <input className={EDIT_INPUT} value={form.username || ''} onChange={(e) => setForm({ ...form, username: e.target.value })} />
              </Field>
              <Field label="Password">
                <input type="password" className={EDIT_INPUT} value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder="Leave blank to keep existing" />
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
                <input className={`flex-1 ${EDIT_INPUT}`} value={form.rtsp_url || ''} onChange={(e) => setForm({ ...form, rtsp_url: e.target.value })} />
                <button type="button" className="px-3 py-2 border border-[var(--border)] bg-[var(--panel-2)] rounded text-xs whitespace-nowrap" onClick={() => setScanQr(true)} title="Scan the QR from the OpenNVR Cam app">Scan QR</button>
              </div>
            </Field>
            <Field label="Substream URL">
              <input className={EDIT_INPUT} value={form.substream_url || ''} onChange={(e) => setForm({ ...form, substream_url: e.target.value })} placeholder="Optional low-res feed for the camera agent's live view" />
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
