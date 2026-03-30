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

type Role = {
  id: number
  name: string
  description?: string
  created_at?: string
  updated_at?: string | null
}

type RoleForm = {
  name: string
  description?: string
}

export function RolesManager() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [roles, setRoles] = useState<Role[]>([])
  const [total, setTotal] = useState(0)
  const [limit, setLimit] = useState(20)
  const [page, setPage] = useState(1)
  const skip = useMemo(() => (page - 1) * limit, [page, limit])

  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [showEditDialog, setShowEditDialog] = useState(false)
  const [editing, setEditing] = useState<Role | null>(null)
  const [form, setForm] = useState<RoleForm>({ name: '', description: '' })

  const canAdmin = !!me?.is_superuser

  useEffect(() => {
    if (!canAdmin) return
    ;(async () => {
      try {
        setLoading(true)
        setError(null)
        const res = await apiService.getRoles()
        const list = (res.data && (res.data as any).roles) ? (res.data as any).roles : (Array.isArray(res.data) ? res.data : [])
        setRoles(list)
        setTotal((res.data && (res.data as any).total) ? (res.data as any).total : list.length)
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load roles')
      } finally {
        setLoading(false)
      }
    })()
  }, [canAdmin, skip, limit])

  const totalPages = Math.max(1, Math.ceil(total / limit))
  const paged = roles.slice(skip, skip + limit)

  const resetForm = () => setForm({ name: '', description: '' })

  const startCreate = () => {
    setShowCreateDialog(true)
    setEditing(null)
    resetForm()
  }

  const startEdit = (r: Role) => {
    setEditing(r)
    setShowCreateDialog(false)
    setForm({ name: r.name, description: r.description || '' })
    setShowEditDialog(true)
  }

  const refresh = async () => {
    const res = await apiService.getRoles()
    const list = (res.data && (res.data as any).roles) ? (res.data as any).roles : (Array.isArray(res.data) ? res.data : [])
    setRoles(list)
    setTotal((res.data && (res.data as any).total) ? (res.data as any).total : list.length)
  }

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      setLoading(true)
      setError(null)
      await apiService.createRole({ name: form.name.trim(), description: form.description?.trim() || undefined })
      setShowCreateDialog(false)
      resetForm()
      await refresh()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to create role')
    } finally {
      setLoading(false)
    }
  }

  const onUpdate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!editing) return
    try {
      setLoading(true)
      setError(null)
      await apiService.updateRole(editing.id, { name: form.name.trim(), description: form.description?.trim() || undefined })
      setShowEditDialog(false)
      setEditing(null)
      resetForm()
      await refresh()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to update role')
    } finally {
      setLoading(false)
    }
  }

  const onDelete = async (r: Role) => {
    if (!confirm(`Delete role "${r.name}"?`)) return
    try {
      setLoading(true)
      setError(null)
      await apiService.deleteRole(r.id)
      await refresh()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to delete role')
    } finally {
      setLoading(false)
    }
  }

  if (!canAdmin) {
    return <div className="text-sm text-amber-400">Admin only: you don’t have permission to manage roles.</div>
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">Roles</h2>
        <div className="ml-auto flex items-center gap-2 text-sm">
          <select className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1" value={limit} onChange={(e) => { setPage(1); setLimit(Number(e.target.value)) }}>
            {[10, 20, 50].map(n => <option key={n} value={n}>{n}/page</option>)}
          </select>
          <button className="px-2 py-1 bg-[var(--accent)] text-white rounded" onClick={startCreate}>Add Role</button>
        </div>
      </div>

      {error && <div className="text-sm text-red-400">{error}</div>}

      {/* Create Role Dialog */}
      {showCreateDialog && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-md w-full mx-4 rounded-lg">
            <h3 className="text-lg font-medium mb-4">Add New Role</h3>
            <form onSubmit={onCreate} className="space-y-3">
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Name</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required minLength={1} maxLength={50} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Description</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.description || ''} onChange={(e) => setForm({ ...form, description: e.target.value })} />
              </label>
              <div className="flex justify-end gap-2 mt-4">
                <button type="button" className="px-4 py-2 border border-neutral-700 bg-[var(--panel-2)] rounded" onClick={() => { setShowCreateDialog(false); resetForm() }}>Cancel</button>
                <button type="submit" className="px-4 py-2 bg-[var(--accent)] text-white rounded" disabled={loading}>{loading ? 'Creating...' : 'Create Role'}</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Edit Role Dialog */}
      {showEditDialog && editing && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-md w-full mx-4 rounded-lg">
            <h3 className="text-lg font-medium mb-4">Edit Role: {editing.name}</h3>
            <form onSubmit={onUpdate} className="space-y-3">
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Name</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required minLength={1} maxLength={50} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Description</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.description || ''} onChange={(e) => setForm({ ...form, description: e.target.value })} />
              </label>
              <div className="flex justify-end gap-2 mt-4">
                <button type="button" className="px-4 py-2 border border-neutral-700 bg-[var(--panel-2)] rounded" onClick={() => { setShowEditDialog(false); setEditing(null); resetForm() }}>Cancel</button>
                <button type="submit" className="px-4 py-2 bg-[var(--accent)] text-white rounded" disabled={loading}>{loading ? 'Updating...' : 'Update Role'}</button>
              </div>
            </form>
          </div>
        </div>
      )}

      <div className="overflow-auto border border-neutral-700">
        <table className="w-full text-sm">
          <thead className="bg-[var(--panel-2)] text-left">
            <tr>
              <th className="p-2">Name</th>
              <th className="p-2">Description</th>
              <th className="p-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {paged.map((r) => (
              <tr key={r.id} className="odd:bg-[var(--bg-2)] even:bg-[var(--panel)]">
                <td className="p-2">{r.name}</td>
                <td className="p-2">{r.description || ''}</td>
                <td className="p-2 space-x-2">
                  <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={() => startEdit(r)}>Edit</button>
                  <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={() => onDelete(r)}>Delete</button>
                </td>
              </tr>
            ))}
            {paged.length === 0 && (
              <tr>
                <td colSpan={3} className="p-3 text-center text-[var(--text-dim)]">No roles</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center gap-2 text-sm">
        <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>Prev</button>
        <span>Page {page} / {totalPages}</span>
        <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>Next</button>
      </div>
    </div>
  )
}


