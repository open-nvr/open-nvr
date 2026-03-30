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

type User = {
  id: number
  username: string
  email: string
  first_name?: string
  last_name?: string
  is_active: boolean
  is_superuser: boolean
  role_id: number
}

type UserForm = {
  username: string
  email: string
  first_name?: string
  last_name?: string
  password?: string
  role_id: number
  is_active?: boolean
}

type Role = {
  id: number
  name: string
  description?: string
}

export function UsersManager() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [users, setUsers] = useState<User[]>([])
  const [total, setTotal] = useState(0)
  const [activeOnly, setActiveOnly] = useState(true)
  const [limit, setLimit] = useState(20)
  const [page, setPage] = useState(1)
  const skip = useMemo(() => (page - 1) * limit, [page, limit])

  const [editing, setEditing] = useState<User | null>(null)
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [showEditDialog, setShowEditDialog] = useState(false)

  const [form, setForm] = useState<UserForm>({ username: '', email: '', password: '', role_id: 1, first_name: '', last_name: '' })
  const [roles, setRoles] = useState<Role[]>([])

  const canAdmin = !!me?.is_superuser

  useEffect(() => {
    if (!canAdmin) return
    ;(async () => {
      try {
        setLoading(true)
        setError(null)
  const { data } = await apiService.getUsers({ skip, limit, active_only: activeOnly })
  setUsers(data.users)
  // Backend returns total count of all users (not filtered) — this can mismatch when active_only=true.
  // Keep it for non-filtered views, but ignore for active-only pagination.
  setTotal(data.total ?? 0)
  // Load roles for dropdown
  const rolesRes = await apiService.getRoles()
  setRoles((rolesRes.data && (rolesRes.data as any).roles) ? (rolesRes.data as any).roles : (Array.isArray(rolesRes.data) ? rolesRes.data : []))
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load users')
      } finally {
        setLoading(false)
      }
    })()
  }, [canAdmin, skip, limit, activeOnly])

  const resetForm = () => setForm({ username: '', email: '', password: '', role_id: 1, first_name: '', last_name: '' })

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      setLoading(true)
      setError(null)
      const payload: any = {
        username: form.username,
        email: form.email,
        password: form.password,
        role_id: Number(form.role_id),
        first_name: form.first_name || null,
        last_name: form.last_name || null,
        is_active: true,
      }
      await apiService.createUser(payload)
      setShowCreateDialog(false)
      resetForm()
      // refresh
  const { data } = await apiService.getUsers({ skip, limit, active_only: activeOnly })
  setUsers(data.users)
  setTotal(data.total ?? 0)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to create user')
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
      const payload: any = {
        username: form.username,
        email: form.email,
        first_name: form.first_name || null,
        last_name: form.last_name || null,
        role_id: Number(form.role_id),
        is_active: form.is_active,
      }
      // Remove undefined fields
      Object.keys(payload).forEach((k) => payload[k] === undefined && delete payload[k])
      await apiService.updateUser(editing.id, payload)
      setShowEditDialog(false)
      setEditing(null)
      resetForm()
  const { data } = await apiService.getUsers({ skip, limit, active_only: activeOnly })
  setUsers(data.users)
  setTotal(data.total ?? 0)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to update user')
    } finally {
      setLoading(false)
    }
  }

  const onDelete = async (u: User) => {
    if (!confirm(`Delete user "${u.username}"?`)) return
    try {
      setLoading(true)
      setError(null)
      await apiService.deleteUser(u.id)
      const { data } = await apiService.getUsers({ skip, limit, active_only: activeOnly })
      setUsers(data.users)
      setTotal(data.total)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to delete user')
    } finally {
      setLoading(false)
    }
  }

  const startEdit = (u: User) => {
    setEditing(u)
    setShowCreateDialog(false)
    setForm({
      username: u.username,
      email: u.email,
      first_name: u.first_name || '',
      last_name: u.last_name || '',
      role_id: u.role_id,
      is_active: u.is_active,
    })
    setShowEditDialog(true)
  }

  if (!canAdmin) {
    return <div className="text-sm text-amber-400">Admin only: you don’t have permission to manage users.</div>
  }

  const totalPages = Math.max(1, Math.ceil(total / limit))
  const hasNext = users.length === limit

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">Users</h2>
        <div className="ml-auto flex items-center gap-2 text-sm">
          <label className="inline-flex items-center gap-1">
            <input type="checkbox" className="accent-[var(--accent)]" checked={activeOnly} onChange={(e) => { setPage(1); setActiveOnly(e.target.checked) }} /> Active only
          </label>
          <select className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1" value={limit} onChange={(e) => { setPage(1); setLimit(Number(e.target.value)) }}>
            {[10, 20, 50].map(n => <option key={n} value={n}>{n}/page</option>)}
          </select>
          <button className="px-2 py-1 bg-[var(--accent)] text-white rounded" onClick={() => { setShowCreateDialog(true); setEditing(null); resetForm() }}>Add User</button>
        </div>
      </div>

      {error && <div className="text-sm text-red-400">{error}</div>}

      {/* Create User Dialog */}
      {showCreateDialog && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-lg w-full mx-4 rounded-lg">
            <h3 className="text-lg font-medium mb-4">Add New User</h3>
            <form onSubmit={onCreate} className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Username</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Email</span>
                <input type="email" className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">First Name</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.first_name || ''} onChange={(e) => setForm({ ...form, first_name: e.target.value })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Last Name</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.last_name || ''} onChange={(e) => setForm({ ...form, last_name: e.target.value })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Password</span>
                <input type="password" className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} required minLength={8} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Role</span>
                <select className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.role_id} onChange={(e) => setForm({ ...form, role_id: Number(e.target.value) })} required>
                  {roles.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
                </select>
              </label>
              <div className="col-span-2 flex justify-end gap-2 mt-4">
                <button type="button" className="px-4 py-2 border border-neutral-700 bg-[var(--panel-2)] rounded" onClick={() => { setShowCreateDialog(false); resetForm() }}>Cancel</button>
                <button type="submit" className="px-4 py-2 bg-[var(--accent)] text-white rounded" disabled={loading}>{loading ? 'Creating...' : 'Create User'}</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Edit User Dialog */}
      {showEditDialog && editing && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-lg w-full mx-4 rounded-lg">
            <h3 className="text-lg font-medium mb-4">Edit User: {editing.username}</h3>
            <form onSubmit={onUpdate} className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Username</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded opacity-50" value={form.username} disabled />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Email</span>
                <input type="email" className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">First Name</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.first_name || ''} onChange={(e) => setForm({ ...form, first_name: e.target.value })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Last Name</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.last_name || ''} onChange={(e) => setForm({ ...form, last_name: e.target.value })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Role</span>
                <select className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.role_id} onChange={(e) => setForm({ ...form, role_id: Number(e.target.value) })} required>
                  {roles.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
                </select>
              </label>
              <label className="flex items-center gap-2 mt-6">
                <input type="checkbox" className="accent-[var(--accent)]" checked={!!form.is_active} onChange={(e) => setForm({ ...form, is_active: e.target.checked })} /> Active
              </label>
              <div className="col-span-2 flex justify-end gap-2 mt-4">
                <button type="button" className="px-4 py-2 border border-neutral-700 bg-[var(--panel-2)] rounded" onClick={() => { setShowEditDialog(false); setEditing(null); resetForm() }}>Cancel</button>
                <button type="submit" className="px-4 py-2 bg-[var(--accent)] text-white rounded" disabled={loading}>{loading ? 'Updating...' : 'Update User'}</button>
              </div>
            </form>
          </div>
        </div>
      )}

      <div className="overflow-auto border border-neutral-700">
        <table className="w-full text-sm">
          <thead className="bg-[var(--panel-2)] text-left">
            <tr>
              <th className="p-2">Username</th>
              <th className="p-2">Email</th>
              <th className="p-2">Role</th>
              <th className="p-2">Active</th>
              <th className="p-2">Superuser</th>
              <th className="p-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className="odd:bg-[var(--bg-2)] even:bg-[var(--panel)]">
                <td className="p-2">{u.username}</td>
                <td className="p-2">{u.email}</td>
                <td className="p-2">{roles.find(r => r.id === u.role_id)?.name ?? u.role_id}</td>
                <td className="p-2">{u.is_active ? 'Yes' : 'No'}</td>
                <td className="p-2">{u.is_superuser ? 'Yes' : 'No'}</td>
                <td className="p-2 space-x-2">
                  <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={() => startEdit(u)}>Edit</button>
                  <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={() => onDelete(u)}>Delete</button>
                </td>
              </tr>
            ))}
            {users.length === 0 && (
              <tr>
                <td colSpan={6} className="p-3 text-center text-[var(--text-dim)]">No users</td>
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
    </div>
  )
}
