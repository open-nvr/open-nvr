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

import { useEffect, useState } from 'react'
import { apiService } from '../../lib/apiService'
import { useAuth } from '../../auth/AuthContext'

type PlatformAccessConfig = {
  allowed_origins?: string[]
  allowed_cidrs?: string[]
  idle_timeout_minutes?: number
  absolute_session_hours?: number
  concurrent_session_limit?: number
  mfa_required_roles?: string[]
}

export function SecurityPlatformAccess() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cfg, setCfg] = useState<PlatformAccessConfig>({})

  const canAdmin = !!me?.is_superuser

  const load = async () => {
    try {
      setLoading(true)
      setError(null)
      const res = await apiService.getSecuritySetting('platform_access')
      const v = (res.data && (res.data as any).value) ? (res.data as any).value : {}
      setCfg({
        allowed_origins: v.allowed_origins || [],
        allowed_cidrs: v.allowed_cidrs || [],
        idle_timeout_minutes: v.idle_timeout_minutes || 30,
        absolute_session_hours: v.absolute_session_hours || 24,
        concurrent_session_limit: v.concurrent_session_limit || 0,
        mfa_required_roles: v.mfa_required_roles || ['admin','operator'],
      })
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load access config')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { if (canAdmin) load() }, [canAdmin])

  const save = async () => {
    try {
      setLoading(true)
      setError(null)
      await apiService.setSecuritySetting('platform_access', cfg as any)
      await load()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save access config')
    } finally {
      setLoading(false)
    }
  }

  if (!canAdmin) return <div className="text-sm text-amber-400">Admin only.</div>

  const listEdit = (label: string, items: string[], onChange: (arr: string[]) => void) => (
    <div className="border border-neutral-700 bg-[var(--panel-2)] p-2">
      <div className="text-[var(--text-dim)] mb-1">{label}</div>
      <div className="space-y-1">
        {items.map((it, i) => (
          <div key={i} className="flex items-center gap-2">
            <input className="flex-1 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={it} onChange={(e)=>{const n=[...items]; n[i]=e.target.value; onChange(n)}} />
            <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={()=>{const n=[...items]; n.splice(i,1); onChange(n)}}>Remove</button>
          </div>
        ))}
        <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={()=>onChange([...items, ''])}>Add</button>
      </div>
    </div>
  )

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">Platform Access</h2>
        <button className="ml-auto px-2 py-1 bg-[var(--accent)] text-white" onClick={save} disabled={loading}>Save</button>
      </div>
      {error && <div className="text-sm text-red-400">{error}</div>}

      <div className="grid grid-cols-2 gap-3 text-sm">
        {listEdit('Allowed Origins', cfg.allowed_origins || [], (arr)=>setCfg({...cfg, allowed_origins: arr}))}
        {listEdit('Allowed Login IP CIDRs', cfg.allowed_cidrs || [], (arr)=>setCfg({...cfg, allowed_cidrs: arr}))}
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Idle timeout (minutes)</span>
          <input type="number" min={1} max={480} className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.idle_timeout_minutes || 30} onChange={(e)=>setCfg({...cfg, idle_timeout_minutes: Number(e.target.value)})} />
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Absolute session lifetime (hours)</span>
          <input type="number" min={1} max={240} className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.absolute_session_hours || 24} onChange={(e)=>setCfg({...cfg, absolute_session_hours: Number(e.target.value)})} />
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Concurrent session limit (0 = unlimited)</span>
          <input type="number" min={0} max={50} className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.concurrent_session_limit || 0} onChange={(e)=>setCfg({...cfg, concurrent_session_limit: Number(e.target.value)})} />
        </label>
        {listEdit('MFA required for roles', cfg.mfa_required_roles || [], (arr)=>setCfg({...cfg, mfa_required_roles: arr}))}
      </div>
    </div>
  )
}


