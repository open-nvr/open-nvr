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
import { apiService } from '../../lib/apiService'
import { useAuth } from '../../auth/AuthContext'
import { useSnackbar } from '../../components/Snackbar'

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
  // New media-server integration fields
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

export function CamerasManager() {
  const { user: me } = useAuth()
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

  // Provisioning dialog state
  const [showProvisionDialog, setShowProvisionDialog] = useState(false)
  const [provisioningCamera, setProvisioningCamera] = useState<Camera | null>(null)
  const [provisionConfig, setProvisionConfig] = useState({
    enable_recording: false,
    rtsp_transport: 'tcp',
    recording_segment_seconds: 300,
    recording_path: ''
  })
  const [showBulkAssign, setShowBulkAssign] = useState(false)
  const [bulkUserId, setBulkUserId] = useState<number | ''>('')
  const [userQuery, setUserQuery] = useState('')
  const [userOptions, setUserOptions] = useState<Array<{ id: number; username: string; email: string; is_active: boolean }>>([])
  const [usersLoading, setUsersLoading] = useState(false)
  const [bulkCanView, setBulkCanView] = useState(true)
  const [bulkCanManage, setBulkCanManage] = useState(false)

  const [editing, setEditing] = useState<Camera | null>(null)
  const [creating, setCreating] = useState(false)
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

  useEffect(() => {
    ; (async () => {
      try {
        setLoading(true)
        const { data } = await apiService.getCameras({ skip, limit, active_only: activeOnly, q: query || undefined })
        setCameras(data.cameras)
        setTotal(data.total ?? 0)
        // Clear selection on reload to avoid mismatches
        setSelected(new Set())
      } catch (e: any) {
        showError(e?.data?.detail || e?.message || 'Failed to load cameras')
      } finally {
        setLoading(false)
      }
    })()
  }, [skip, limit, activeOnly, query])

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
      setCreating(false)
      resetForm()
      const { data } = await apiService.getCameras({ skip, limit, active_only: activeOnly, q: query || undefined })
      setCameras(data.cameras)
      setTotal(data.total ?? 0)

      // Show appropriate success message based on media-server provisioning result
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
        // only send password if provided
        ...(form.password ? { password: form.password } : {}),
        rtsp_url: form.rtsp_url || null,
        location: form.location || null,
        vlan: form.vlan || null,
        status: form.status || undefined,
        is_active: form.is_active,
      }
      // prune undefined to avoid accidental overwrites
      Object.keys(payload).forEach((k) => payload[k] === undefined && delete payload[k])
      await apiService.updateCamera(editing.id, payload)
      setShowEditDialog(false)
      setEditing(null)
      resetForm()
      const { data } = await apiService.getCameras({ skip, limit, active_only: activeOnly, q: query || undefined })
      setCameras(data.cameras)
      setTotal(data.total ?? 0)
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
      const { data } = await apiService.getCameras({ skip, limit, active_only: activeOnly, q: query || undefined })
      setCameras(data.cameras)
      setTotal(data.total ?? 0)
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
      // Sequential to keep server load predictable
      for (const id of ids) {
        try {
          await apiService.deleteCamera(id)
        } catch (err) {
          // accumulate but continue
        }
      }
      const { data } = await apiService.getCameras({ skip, limit, active_only: activeOnly, q: query || undefined })
      setCameras(data.cameras)
      setTotal(data.total ?? 0)
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
        } catch (err) {
          // continue
        }
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

  // Typeahead search for users (superuser only)
  useEffect(() => {
    let alive = true
    const run = async () => {
      if (!userQuery) { setUserOptions([]); return }
      try {
        setUsersLoading(true)
        const { data } = await apiService.getUsers({ q: userQuery, limit: 10, active_only: true })
        // data.users is expected; map to lightweight list if needed
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

  const onTest = async (c: Camera) => {
    try {
      setLoading(true)
      const { data } = await apiService.testCameraConnection(c.id)
      showSuccess(data?.message || data?.status || 'Test executed')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to test camera')
    } finally {
      setLoading(false)
    }
  }

  const openProvisionDialog = (camera: Camera) => {
    setProvisioningCamera(camera)
    setProvisionConfig({
      enable_recording: false,
      rtsp_transport: 'tcp',
      recording_segment_seconds: 300,
      recording_path: ''
    })
    setShowProvisionDialog(true)
  }

  const onProvisionMediaMTX = async () => {
    if (!provisioningCamera) return

    try {
      setLoading(true)

      const { data } = await apiService.provisionCameraMediaMTX(provisioningCamera.id, provisionConfig)

      if (data.mediamtx_result?.status === 'ok') {
        showSuccess(`Camera "${provisioningCamera.name}" successfully provisioned in Media Server!${provisionConfig.enable_recording ? ' Recording is enabled.' : ''}`)
        // Refresh camera list to show updated status
        const { data: camerasData } = await apiService.getCameras({ skip, limit, active_only: activeOnly, q: query || undefined })
        setCameras(camerasData.cameras)
        setTotal(camerasData.total ?? 0)
        setShowProvisionDialog(false)
        setProvisioningCamera(null)
      } else {
        showError('Failed to provision camera: ' + (data.mediamtx_result?.details?.error || 'Unknown error'))
      }
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to provision camera in Media Server')
    } finally {
      setLoading(false)
    }
  }

  const onCheckMediaMTXStatus = async (c: Camera) => {
    try {
      setLoading(true)

      const { data } = await apiService.getCameraMediaMTXStatus(c.id)

      const statusMessages = []
      statusMessages.push(`Path configured: ${data.path_configured ? '✓' : '✗'}`)
      statusMessages.push(`Stream active: ${data.path_active ? '✓' : '✗'}`)
      statusMessages.push(`Recording: ${data.recording_status?.recording_enabled ? '✓ Enabled' : '✗ Disabled'}`)

      showInfo(`Media Server Status for "${c.name}": ${statusMessages.join(', ')}`)
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to check Media Server status')
    } finally {
      setLoading(false)
    }
  }

  const startEdit = (c: Camera) => {
    setEditing(c)
    setCreating(false)
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
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">Manage Cameras</h2>
        <div className="ml-auto flex items-center gap-2 text-sm">
          <input
            className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"
            placeholder="Search name or IP"
            value={query}
            onChange={(e) => { setPage(1); setQuery(e.target.value) }}
          />
          {selected.size > 0 && (
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
          <button className="px-2 py-1 bg-[var(--accent)] text-white" onClick={() => { setCreating(true); setEditing(null); resetForm() }}>Add Camera</button>
        </div>
      </div>

      {showBulkAssign && selected.size > 0 && (
        <div className="border border-neutral-700 bg-[var(--panel-2)] p-3 text-sm flex items-center gap-3">
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

      {creating && (
        <div className="border border-neutral-700 bg-[var(--panel-2)] p-3 text-sm">
          <form onSubmit={onCreate} className="grid grid-cols-2 gap-2">
            <Field label="Name">
              <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
            </Field>
            <Field label="IP Address">
              <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={form.ip_address} onChange={(e) => setForm({ ...form, ip_address: e.target.value })} required />
            </Field>
            <Field label="Port">
              <input type="number" className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={form.port} onChange={(e) => setForm({ ...form, port: Number(e.target.value) })} min={1} max={65535} />
            </Field>
            <Field label="RTSP URL">
              <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={form.rtsp_url || ''} onChange={(e) => setForm({ ...form, rtsp_url: e.target.value })} />
            </Field>
            <Field label="Username">
              <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={form.username || ''} onChange={(e) => setForm({ ...form, username: e.target.value })} />
            </Field>
            <Field label="Password">
              <input type="password" className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} />
            </Field>
            <Field label="Location">
              <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={form.location || ''} onChange={(e) => setForm({ ...form, location: e.target.value })} />
            </Field>
            <Field label="VLAN">
              <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={form.vlan || ''} onChange={(e) => setForm({ ...form, vlan: e.target.value })} />
            </Field>
            <Field label="Description">
              <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={form.description || ''} onChange={(e) => setForm({ ...form, description: e.target.value })} />
            </Field>
            <div className="col-span-2 flex items-center gap-2 mt-2">
              <button className="px-3 py-1 bg-[var(--accent)] text-white" disabled={loading}>Create</button>
              <button type="button" className="px-3 py-1 bg-[var(--panel)] border border-neutral-700" onClick={() => { setCreating(false); resetForm() }}>Cancel</button>
            </div>
          </form>
        </div>
      )}

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
              <th className="p-2">Media Server</th>
              <th className="p-2">Recording</th>
              {me?.is_superuser && <th className="p-2">Owner ID</th>}
              <th className="p-2">Active</th>
              <th className="p-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {cameras.map((c) => (
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
                  <span className={`inline-flex items-center gap-1 text-xs px-2 py-1 rounded ${c.mediamtx_provisioned === true ? 'bg-green-900/50 text-green-400' :
                      c.mediamtx_provisioned === false ? 'bg-red-900/50 text-red-400' :
                        'bg-gray-900/50 text-gray-400'
                    }`}>
                    {c.mediamtx_provisioned === true ? '✓ Provisioned' :
                      c.mediamtx_provisioned === false ? '✗ Failed' :
                        '— Not set'}
                  </span>
                </td>
                <td className="p-2">
                  <span className={`inline-flex items-center gap-1 text-xs px-2 py-1 rounded ${c.recording_enabled === true ? 'bg-red-900/50 text-red-400' :
                      c.recording_enabled === false ? 'bg-gray-900/50 text-gray-400' :
                        'bg-gray-900/50 text-gray-400'
                    }`}>
                    {c.recording_enabled === true ? '🔴 Recording' :
                      c.recording_enabled === false ? '⚫ Off' :
                        '— Not set'}
                  </span>
                </td>
                {me?.is_superuser && <td className="p-2">{c.owner_id}</td>}
                <td className="p-2">{c.is_active ? 'Yes' : 'No'}</td>
                <td className="p-2">
                  <div className="flex flex-wrap items-center gap-1">
                    <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)] text-xs" onClick={() => startEdit(c)}>Edit</button>
                    {/* <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)] text-xs" onClick={() => onTest(c)}>Test</button> */}
                    {c.rtsp_url && (
                      <>
                        {/* <button
                          className="px-2 py-1 border border-green-600 bg-green-900/20 text-green-400 text-xs"
                          onClick={() => openProvisionDialog(c)}
                          title="Configure and provision in Media Server"
                        >
                          Provision
                        </button> */}
                        {c.mediamtx_provisioned && (
                          <button
                            className={`px-2 py-1 border text-xs ${c.recording_enabled ? 'border-red-600 bg-red-900/20 text-red-400' : 'border-neutral-600 bg-neutral-800 text-neutral-400'}`}
                            onClick={async () => {
                              try {
                                setLoading(true)
                                const { data } = await apiService.toggleCameraRecording(c.id, !c.recording_enabled)
                                if (data.status === 'ok' || data.recording_enabled !== undefined) {
                                  showSuccess(`Recording ${!c.recording_enabled ? 'enabled' : 'disabled'} for ${c.name}`)
                                  // Refresh list
                                  const { data: camerasData } = await apiService.getCameras({ skip, limit, active_only: activeOnly, q: query || undefined })
                                  setCameras(camerasData.cameras)
                                } else {
                                  showError('Failed to toggle recording')
                                }
                              } catch (e: any) {
                                showError(e?.data?.detail || e?.message || 'Failed to toggle recording')
                              } finally {
                                setLoading(false)
                              }
                            }}
                            title={c.recording_enabled ? "Disable Recording" : "Enable Recording"}
                          >
                            {c.recording_enabled ? "Stop Rec" : "Start Rec"}
                          </button>
                        )}
                        {/* <button
                          className="px-2 py-1 border border-blue-600 bg-blue-900/20 text-blue-400 text-xs"
                          onClick={() => onCheckMediaMTXStatus(c)}
                          title="Check Media Server status"
                        >
                          Status
                        </button> */}
                      </>
                    )}
                    <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)] text-xs" onClick={() => onDelete(c)}>Delete</button>
                  </div>
                </td>
              </tr>
            ))}
            {cameras.length === 0 && (
              <tr>
                <td colSpan={me?.is_superuser ? 10 : 9} className="p-3 text-center text-[var(--text-dim)]">No cameras</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

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
      {showEditDialog && editing && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-lg w-full mx-4">
            <h3 className="text-lg font-medium mb-4">Edit Camera: {editing.name}</h3>
            <form onSubmit={onUpdate} className="grid grid-cols-2 gap-3">
              <Field label="Name">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
              </Field>
              <Field label="IP Address">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2" value={form.ip_address} onChange={(e) => setForm({ ...form, ip_address: e.target.value })} required />
              </Field>
              <Field label="Port">
                <input type="number" className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2" value={form.port} onChange={(e) => setForm({ ...form, port: Number(e.target.value) })} min={1} max={65535} />
              </Field>
              <Field label="RTSP URL">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2" value={form.rtsp_url || ''} onChange={(e) => setForm({ ...form, rtsp_url: e.target.value })} />
              </Field>
              <Field label="Username">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2" value={form.username || ''} onChange={(e) => setForm({ ...form, username: e.target.value })} />
              </Field>
              <Field label="Password">
                <input type="password" className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2" value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder="Leave blank to keep existing" />
              </Field>
              <Field label="Location">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2" value={form.location || ''} onChange={(e) => setForm({ ...form, location: e.target.value })} />
              </Field>
              <Field label="VLAN">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2" value={form.vlan || ''} onChange={(e) => setForm({ ...form, vlan: e.target.value })} />
              </Field>
              <Field label="Description">
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2" value={form.description || ''} onChange={(e) => setForm({ ...form, description: e.target.value })} />
              </Field>
              <label className="flex items-center gap-2 mt-2">
                <input type="checkbox" className="accent-[var(--accent)]" checked={!!form.is_active} onChange={(e) => setForm({ ...form, is_active: e.target.checked })} /> Active
              </label>
              <div className="col-span-2 flex justify-end gap-2 mt-4">
                <button type="button" className="px-4 py-2 border border-neutral-700 bg-[var(--panel-2)]" onClick={closeEditDialog}>Cancel</button>
                <button type="submit" className="px-4 py-2 bg-[var(--accent)] text-white" disabled={loading}>{loading ? 'Updating...' : 'Update'}</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Provisioning Dialog */}
      {showProvisionDialog && provisioningCamera && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-medium mb-4">Provision Camera: {provisioningCamera.name}</h3>

            <div className="space-y-4">
              <Field label="RTSP Transport">
                <select
                  className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2"
                  value={provisionConfig.rtsp_transport}
                  onChange={(e) => setProvisionConfig(prev => ({ ...prev, rtsp_transport: e.target.value }))}
                >
                  <option value="tcp">TCP (Recommended)</option>
                  <option value="udp">UDP</option>
                  <option value="auto">Auto</option>
                </select>
              </Field>

              <div className="flex items-center gap-2">
                <label className="inline-flex items-center gap-2">
                  <input
                    type="checkbox"
                    className="accent-[var(--accent)]"
                    checked={provisionConfig.enable_recording}
                    onChange={(e) => setProvisionConfig(prev => ({ ...prev, enable_recording: e.target.checked }))}
                  />
                  Enable Recording
                </label>
              </div>

              {provisionConfig.enable_recording && (
                <>
                  <Field label="Recording Segment Duration">
                    <select
                      className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2"
                      value={provisionConfig.recording_segment_seconds}
                      onChange={(e) => setProvisionConfig(prev => ({ ...prev, recording_segment_seconds: Number(e.target.value) }))}
                    >
                      <option value="60">1 minute</option>
                      <option value="300">5 minutes (default)</option>
                      <option value="600">10 minutes</option>
                      <option value="1800">30 minutes</option>
                      <option value="3600">1 hour</option>
                    </select>
                  </Field>

                  <Field label="Recording Path (Optional)">
                    <input
                      className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 w-full"
                      value={provisionConfig.recording_path}
                      onChange={(e) => setProvisionConfig(prev => ({ ...prev, recording_path: e.target.value }))}
                      placeholder="e.g., D:\recordings\cam-20\%Y\%m\%d\%H-%M-%S-%f"
                    />
                    <div className="text-xs text-neutral-400 mt-1">
                      Leave empty for default path. Use %Y, %m, %d, %H, %M, %S, %f for timestamps.
                    </div>
                  </Field>
                </>
              )}
            </div>

            <div className="flex justify-end gap-2 mt-6">
              <button
                className="px-4 py-2 border border-neutral-700 bg-[var(--panel-2)]"
                onClick={() => {
                  setShowProvisionDialog(false)
                  setProvisioningCamera(null)
                }}
              >
                Cancel
              </button>
              <button
                className="px-4 py-2 bg-[var(--accent)] text-white"
                onClick={onProvisionMediaMTX}
                disabled={loading}
              >
                {loading ? 'Provisioning...' : 'Provision'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
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
