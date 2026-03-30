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
import { Unplug } from 'lucide-react'

type Camera = {
  id: number
  name: string
  description?: string | null
  ip_address: string
  port: number
  username?: string | null
  password?: string | null
  rtsp_url?: string | null
  location?: string | null
  vlan?: string | null
  status?: string | null
  owner_id: number
  is_active: boolean
  mediamtx_provisioned?: boolean | null
  recording_enabled?: boolean | null
  // ONVIF device metadata
  manufacturer?: string | null
  model?: string | null
  firmware_version?: string | null
  serial_number?: string | null
  hardware_id?: string | null
}

type CameraForm = {
  name: string
  description?: string
  ip_address: string
  port: number
  username?: string
  password?: string
  rtsp_url?: string
  location?: string
  vlan?: string
  status?: string
  is_active?: boolean
}

export function Cameras() {
  const navigate = useNavigate()
  const { token, loading: authLoading, user: me } = useAuth()
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

  const [form, setForm] = useState<CameraForm>({
    name: '',
    description: '',
    ip_address: '',
    port: 554,
    username: '',
    password: '',
    rtsp_url: '',
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
    location: '',
    vlan: '',
    status: 'unknown',
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

  // Load cameras
  useEffect(() => {
    if (authLoading) return
    let alive = true
    ;(async () => {
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
    })()
    return () => { alive = false }
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

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      setLoading(true)
      const payload: any = {
        name: form.name,
        description: form.description || null,
        ip_address: form.ip_address,
        port: Number(form.port) || 554,
        username: form.username || null,
        password: form.password || null,
        rtsp_url: form.rtsp_url || null,
        location: form.location || null,
        vlan: form.vlan || null,
        status: form.status || 'unknown',
      }
      const response = await apiService.createCamera(payload)
      setShowCreateDialog(false)
      resetForm()
      await refreshCameras()

      const camera = response.data
      if (camera.mediamtx_provisioned === true) {
        showSuccess(`Camera created and automatically configured for streaming!${camera.recording_enabled ? ' Recording is enabled.' : ''}`)
      } else if (camera.mediamtx_provisioned === false) {
        showInfo(`Camera created but Media Server configuration failed. You can manually provision it from the camera actions.`)
      } else {
        showSuccess('Camera created. Add an RTSP URL and provision it to enable streaming.')
      }
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to create camera')
    } finally {
      setLoading(false)
    }
  }

  const onUpdate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!editing) return
    try {
      setLoading(true)
      const payload: any = {
        name: form.name,
        description: form.description || null,
        ip_address: form.ip_address,
        port: Number(form.port) || undefined,
        username: form.username || null,
        ...(form.password ? { password: form.password } : {}),
        rtsp_url: form.rtsp_url || null,
        location: form.location || null,
        vlan: form.vlan || null,
        status: form.status || undefined,
        is_active: form.is_active,
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
      setLoading(false)
    }
  }

  const onDelete = async (c: Camera) => {
    if (!confirm(`Delete camera "${c.name}"?`)) return
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
    if (!confirm(`Delete ${ids.length} selected camera(s)?`)) return
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
      location: c.location || '',
      vlan: c.vlan || '',
      status: c.status || 'unknown',
      is_active: c.is_active,
    })
    setShowEditDialog(true)
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
      <div className="flex items-center gap-2 flex-wrap">
        <h1 className="text-lg font-semibold">Cameras</h1>
        <div className="ml-auto flex items-center gap-2 text-sm flex-wrap">
          <input
            className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"
            placeholder="Search name or IP"
            value={query}
            onChange={(e) => { setPage(1); setQuery(e.target.value) }}
          />
          {canManageCameras && selected.size > 0 && (
            <div className="flex items-center gap-2">
              <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={onBulkDelete} disabled={loading}>
                Delete Selected ({selected.size})
              </button>
              <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={() => setShowBulkAssign((s) => !s)} disabled={loading}>
                Assign Permissions
              </button>
            </div>
          )}
          <label className="inline-flex items-center gap-1">
            <input type="checkbox" className="accent-[var(--accent)]" checked={activeOnly} onChange={(e) => { setPage(1); setActiveOnly(e.target.checked) }} /> Active only
          </label>
          <select className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1" value={limit} onChange={(e) => { setPage(1); setLimit(Number(e.target.value)) }}>
            {[10, 20, 50].map(n => <option key={n} value={n}>{n}/page</option>)}
          </select>
          {canManageCameras && (
            <button className="px-2 py-1 bg-[var(--accent)] text-white" onClick={() => { setShowCreateDialog(true); setEditing(null); resetForm() }}>Add Camera</button>
          )}
        </div>
      </div>

      {/* Bulk Assign Panel */}
      {canManageCameras && showBulkAssign && selected.size > 0 && (
        <div className="border border-neutral-700 bg-[var(--panel-2)] p-3 text-sm flex items-center gap-3 flex-wrap">
          <div className="text-[var(--text-dim)]">Assign to user</div>
          <div className="relative">
            <input
              className="bg-[var(--panel)] border border-neutral-700 px-2 py-1 w-56"
              placeholder="Type username or email"
              value={userQuery}
              onChange={(e) => { setUserQuery(e.target.value); setBulkUserId('') }}
            />
            {userQuery && (
              <div className="absolute z-10 mt-1 w-full bg-[var(--panel)] border border-neutral-700 max-h-56 overflow-auto">
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
          <button className="px-3 py-1 bg-[var(--panel)] border border-neutral-700" onClick={() => setShowBulkAssign(false)}>Cancel</button>
        </div>
      )}

      {/* Create Camera Dialog */}
      {canManageCameras && showCreateDialog && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-lg w-full mx-4 rounded-lg">
            <h3 className="text-lg font-medium mb-4">Add New Camera</h3>
            <form onSubmit={onCreate} className="grid grid-cols-2 gap-3">
              <Field label="Name">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
              </Field>
              <Field label="IP Address">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.ip_address} onChange={(e) => setForm({ ...form, ip_address: e.target.value })} required />
              </Field>
              <Field label="Port">
                <input type="number" className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.port} onChange={(e) => setForm({ ...form, port: Number(e.target.value) })} min={1} max={65535} />
              </Field>
              <Field label="RTSP URL">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.rtsp_url || ''} onChange={(e) => setForm({ ...form, rtsp_url: e.target.value })} />
              </Field>
              <Field label="Username">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.username || ''} onChange={(e) => setForm({ ...form, username: e.target.value })} />
              </Field>
              <Field label="Password">
                <input type="password" className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} />
              </Field>
              <Field label="Location">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.location || ''} onChange={(e) => setForm({ ...form, location: e.target.value })} />
              </Field>
              <Field label="VLAN">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.vlan || ''} onChange={(e) => setForm({ ...form, vlan: e.target.value })} />
              </Field>
              <Field label="Description">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.description || ''} onChange={(e) => setForm({ ...form, description: e.target.value })} />
              </Field>
              <div className="col-span-2 flex justify-end gap-2 mt-4">
                <button type="button" className="px-4 py-2 border border-neutral-700 bg-[var(--panel-2)] rounded" onClick={() => { setShowCreateDialog(false); resetForm() }}>Cancel</button>
                <button type="submit" className="px-4 py-2 bg-[var(--accent)] text-white rounded" disabled={loading}>{loading ? 'Creating...' : 'Create Camera'}</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Cameras Table */}
      <div className="overflow-auto border border-neutral-700">
        <table className="w-full text-sm">
          <thead className="bg-[var(--panel-2)] text-left">
            <tr>
              <th className="p-2 w-8">
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
              </th>
              <th className="p-2">Name</th>
              <th className="p-2">IP</th>
              <th className="p-2">Port</th>
              <th className="p-2">Manufacturer</th>
              <th className="p-2">Model</th>
              <th className="p-2">Serial #</th>
              <th className="p-2">Firmware</th>
              <th className="p-2">Status</th>
              <th className="p-2">Stream Status</th>
              <th className="p-2">Recording</th>
              {me?.is_superuser && <th className="p-2">Owner ID</th>}
              <th className="p-2">Active</th>
              <th className="p-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td className="p-2 text-[var(--text-dim)]" colSpan={14}>Loading…</td>
              </tr>
            ) : cameras.length > 0 ? (
              cameras.map((c) => (
                <tr key={c.id} className="odd:bg-[var(--bg-2)] even:bg-[var(--panel)]">
                  <td className="p-2">
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
                  </td>
                  <td className="p-2">{c.name}</td>
                  <td className="p-2">{c.ip_address}</td>
                  <td className="p-2">{c.port}</td>
                  <td className="p-2 text-xs text-[var(--text-dim)]">{c.manufacturer || '—'}</td>
                  <td className="p-2 text-xs text-[var(--text-dim)]">{c.model || '—'}</td>
                  <td className="p-2 text-xs text-[var(--text-dim)]" title={c.serial_number || undefined}>{c.serial_number ? (c.serial_number.length > 12 ? c.serial_number.slice(0, 12) + '…' : c.serial_number) : '—'}</td>
                  <td className="p-2 text-xs text-[var(--text-dim)]">{c.firmware_version || '—'}</td>
                  <td className="p-2">
                    <span className={`inline-flex items-center gap-1 text-xs px-2 py-1 rounded ${c.status === 'provisioned' ? 'bg-green-900/50 text-green-400' :
                      c.status === 'unknown' ? 'bg-gray-900/50 text-gray-400' :
                        'bg-yellow-900/50 text-yellow-400'
                      }`}>
                      {c.status || 'unknown'}
                    </span>
                  </td>
                  <td className="p-2">
                    {mediamtxAvailable === false ? (
                      <span className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-amber-900/50 text-amber-400" title="Media Server is not running">
                        <Unplug size={12} />
                        Disconnected
                      </span>
                    ) : (() => {
                      const status = streamStatuses[c.id]
                      const isStreaming = status?.ready && status?.bytesReceived > 0
                      
                      if (isStreaming) {
                        return (
                          <span className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-green-900/50 text-green-400">
                            ✓ Ready
                          </span>
                        )
                      } else if (c.mediamtx_provisioned === true) {
                        return (
                          <span className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-amber-900/50 text-amber-400" title="Provisioned but not streaming">
                            ⚠ Disconnected
                          </span>
                        )
                      } else if (c.mediamtx_provisioned === false) {
                        return (
                          <span className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-red-900/50 text-red-400">
                            ✗ Error
                          </span>
                        )
                      } else {
                        return (
                          <span className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-gray-900/50 text-gray-400">
                            — Not configured
                          </span>
                        )
                      }
                    })()}
                  </td>
                  <td className="p-2">
                    <span className={`inline-flex items-center gap-1 text-xs px-2 py-1 rounded ${c.recording_enabled === true ? 'bg-red-900/50 text-red-400' :
                      'bg-gray-900/50 text-gray-400'
                      }`}>
                      {c.recording_enabled === true ? '🔴 Recording' : '⚫ Off'}
                    </span>
                  </td>
                  {me?.is_superuser && <td className="p-2">{c.owner_id}</td>}
                  <td className="p-2">{c.is_active ? 'Yes' : 'No'}</td>
                  <td className="p-2">
                    <div className="flex items-center gap-1">
                      {canManageCameras && (
                        <button
                          className="p-1.5 border border-neutral-700 bg-[var(--panel-2)] hover:bg-neutral-700 transition-colors rounded"
                          onClick={() => startEdit(c)}
                          title="Edit Camera"
                        >
                          <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                          </svg>
                        </button>
                      )}
                      {c.mediamtx_provisioned === true && (
                        <>
                          {canManageCameras && (
                            <button
                              className={`p-1.5 border rounded transition-colors ${c.recording_enabled ? 'border-red-600 bg-red-900/20 text-red-400 hover:bg-red-900/40' : 'border-neutral-600 bg-neutral-800 text-neutral-400 hover:bg-neutral-700'}`}
                              onClick={async () => {
                                const targetState = !c.recording_enabled
                                try {
                                  // Optimistic update
                                  setCameras(prev => prev.map(cam => cam.id === c.id ? { ...cam, recording_enabled: targetState } : cam))
                                  
                                  const { data } = await apiService.toggleCameraRecording(c.id, targetState)
                                  if (data.status === 'ok' || data.recording_enabled !== undefined) {
                                    if (targetState) {
                                      showSuccess(`Recording enabled for ${c.name}`)
                                    } else {
                                      showError(`Recording disabled for ${c.name}`)
                                    }
                                    await refreshCameras()
                                  } else {
                                    // Revert
                                    setCameras(prev => prev.map(cam => cam.id === c.id ? { ...cam, recording_enabled: !targetState } : cam))
                                    showError('Failed to toggle recording')
                                  }
                                } catch (e: any) {
                                  // Revert
                                  setCameras(prev => prev.map(cam => cam.id === c.id ? { ...cam, recording_enabled: !targetState } : cam))
                                  showError(e?.data?.detail || e?.message || 'Failed to toggle recording')
                                }
                              }}
                              title={c.recording_enabled ? "Stop Recording" : "Start Recording"}
                            >
                              {c.recording_enabled ? (
                                <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                                  <rect x="6" y="6" width="12" height="12" rx="1" />
                                </svg>
                              ) : (
                                <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                                  <circle cx="12" cy="12" r="8" />
                                </svg>
                              )}
                            </button>
                          )}
                          <button
                            className="p-1.5 border border-blue-600 bg-blue-900/20 text-blue-400 hover:bg-blue-900/40 transition-colors rounded"
                            onClick={() => navigate(`/live?camera=${c.id}`)}
                            title="View Live"
                          >
                            <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
                            </svg>
                          </button>
                        </>
                      )}
                      {canManageCameras && (
                        <button
                          className="p-1.5 border border-neutral-700 bg-[var(--panel-2)] hover:bg-red-900/40 hover:border-red-600 hover:text-red-400 transition-colors rounded"
                          onClick={() => onDelete(c)}
                          title="Delete Camera"
                        >
                          <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                          </svg>
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td className="p-2 text-[var(--text-dim)]" colSpan={14}>No cameras</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      <div className="flex items-center gap-2 text-sm">
        <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>Prev</button>
        {activeOnly ? (
          <span>Page {page}</span>
        ) : (
          <span>Page {page} / {totalPages}</span>
        )}
        <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" disabled={activeOnly ? !hasNext : page >= totalPages} onClick={() => setPage((p) => p + 1)}>Next</button>
      </div>

      {/* Edit Camera Dialog */}
      {canManageCameras && showEditDialog && editing && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-lg w-full mx-4 rounded-lg">
            <h3 className="text-lg font-medium mb-4">Edit Camera: {editing.name}</h3>
            <form onSubmit={onUpdate} className="grid grid-cols-2 gap-3">
              <Field label="Name">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
              </Field>
              <Field label="IP Address">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.ip_address} onChange={(e) => setForm({ ...form, ip_address: e.target.value })} required />
              </Field>
              <Field label="Port">
                <input type="number" className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.port} onChange={(e) => setForm({ ...form, port: Number(e.target.value) })} min={1} max={65535} />
              </Field>
              <Field label="RTSP URL">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.rtsp_url || ''} onChange={(e) => setForm({ ...form, rtsp_url: e.target.value })} />
              </Field>
              <Field label="Username">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.username || ''} onChange={(e) => setForm({ ...form, username: e.target.value })} />
              </Field>
              <Field label="Password">
                <input type="password" className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder="Leave blank to keep existing" />
              </Field>
              <Field label="Location">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.location || ''} onChange={(e) => setForm({ ...form, location: e.target.value })} />
              </Field>
              <Field label="VLAN">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.vlan || ''} onChange={(e) => setForm({ ...form, vlan: e.target.value })} />
              </Field>
              <Field label="Description">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.description || ''} onChange={(e) => setForm({ ...form, description: e.target.value })} />
              </Field>
              <label className="flex items-center gap-2 mt-2">
                <input type="checkbox" className="accent-[var(--accent)]" checked={!!form.is_active} onChange={(e) => setForm({ ...form, is_active: e.target.checked })} /> Active
              </label>
              <div className="col-span-2 flex justify-end gap-2 mt-4">
                <button type="button" className="px-4 py-2 border border-neutral-700 bg-[var(--panel-2)] rounded" onClick={closeEditDialog}>Cancel</button>
                <button type="submit" className="px-4 py-2 bg-[var(--accent)] text-white rounded" disabled={loading}>{loading ? 'Updating...' : 'Update'}</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </section>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[var(--text-dim)]">{label}</span>
      {children}
    </label>
  )
}
