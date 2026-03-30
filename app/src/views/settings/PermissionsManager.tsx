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

type Role = { id: number; name: string; description?: string }
type Permission = { id: number; name: string; description?: string }

export function PermissionsManager() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [roles, setRoles] = useState<Role[]>([])
  const [permissions, setPermissions] = useState<Permission[]>([])
  const [selectedRoleId, setSelectedRoleId] = useState<number | null>(null)
  const [assignedIds, setAssignedIds] = useState<number[]>([])

  const canAdmin = !!me?.is_superuser

  const selectedRole = useMemo(() => roles.find(r => r.id === selectedRoleId) || null, [roles, selectedRoleId])

  useEffect(() => {
    if (!canAdmin) return
    ;(async () => {
      try {
        setLoading(true)
        setError(null)
        const [rolesRes, permsRes] = await Promise.all([
          apiService.getRoles(),
          apiService.getPermissions(),
        ])
        const rolesList = (rolesRes.data && (rolesRes.data as any).roles) ? (rolesRes.data as any).roles : (Array.isArray(rolesRes.data) ? rolesRes.data : [])
        const permsList = (permsRes.data && (permsRes.data as any).permissions) ? (permsRes.data as any).permissions : (Array.isArray(permsRes.data) ? permsRes.data : [])
        setRoles(rolesList)
        setPermissions(permsList)
        if (!selectedRoleId && rolesList.length > 0) {
          setSelectedRoleId(rolesList[0].id)
        }
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load roles/permissions')
      } finally {
        setLoading(false)
      }
    })()
  }, [canAdmin])

  useEffect(() => {
    if (!canAdmin || !selectedRoleId) return
    ;(async () => {
      try {
        setLoading(true)
        setError(null)
        const res = await apiService.getRolePermissions(selectedRoleId)
        const list = (res.data && (res.data as any).permissions) ? (res.data as any).permissions : (Array.isArray(res.data) ? res.data : [])
        setAssignedIds(list.map((p: Permission) => p.id))
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load role permissions')
      } finally {
        setLoading(false)
      }
    })()
  }, [canAdmin, selectedRoleId])

  const toggle = (pid: number) => {
    setAssignedIds((prev) => prev.includes(pid) ? prev.filter(id => id !== pid) : [...prev, pid])
  }

  const save = async () => {
    if (!selectedRoleId) return
    try {
      setLoading(true)
      setError(null)
      await apiService.setRolePermissions(selectedRoleId, assignedIds)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save permissions')
    } finally {
      setLoading(false)
    }
  }

  if (!canAdmin) {
    return <div className="text-sm text-amber-400">Admin only: you don’t have permission to manage permissions.</div>
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">Permissions</h2>
        <div className="ml-auto text-sm flex items-center gap-2">
          <span className="text-[var(--text-dim)]">Role</span>
          <select className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1" value={selectedRoleId ?? ''} onChange={(e) => setSelectedRoleId(Number(e.target.value))}>
            {roles.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
          <button className="px-2 py-1 bg-[var(--accent)] text-white" onClick={save} disabled={loading || !selectedRoleId}>Save</button>
        </div>
      </div>

      {error && <div className="text-sm text-red-400">{error}</div>}

      <div className="grid grid-cols-2 gap-3 text-sm">
        {permissions.map(p => (
          <label key={p.id} className="flex items-center gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
            <input type="checkbox" className="accent-[var(--accent)]" checked={assignedIds.includes(p.id)} onChange={() => toggle(p.id)} />
            <div>
              <div className="font-medium">{p.name}</div>
              <div className="text-[var(--text-dim)]">{p.description || ''}</div>
            </div>
          </label>
        ))}
        {permissions.length === 0 && (
          <div className="text-[var(--text-dim)]">No permissions defined.</div>
        )}
      </div>
    </div>
  )
}


