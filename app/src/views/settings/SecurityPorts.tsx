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

type PortsConfig = {
  services: { key: string; name: string; internal_port: number; external_port?: number | null; protocol?: 'tcp'|'udp'|'any' }[]
  https_enabled?: boolean
  redirect_http_to_https?: boolean
  tls_cert_issuer?: string
  tls_expiry?: string
}

export function SecurityPorts() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cfg, setCfg] = useState<PortsConfig>({ services: [] })

  const canAdmin = !!me?.is_superuser

  const load = async () => {
    try {
      setLoading(true)
      setError(null)
      const res = await apiService.getSecuritySetting('ports')
      const v = (res.data && (res.data as any).value) ? (res.data as any).value : {}
      setCfg({ services: v.services || [], https_enabled: v.https_enabled || false, redirect_http_to_https: v.redirect_http_to_https || false, tls_cert_issuer: v.tls_cert_issuer || '', tls_expiry: v.tls_expiry || '' })
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load ports')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { if (canAdmin) load() }, [canAdmin])

  const save = async () => {
    try {
      setLoading(true)
      setError(null)
      await apiService.setSecuritySetting('ports', cfg as any)
      await load()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save ports')
    } finally {
      setLoading(false)
    }
  }

  if (!canAdmin) return <div className="text-sm text-amber-400">Admin only.</div>

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">Port Settings</h2>
        <button className="ml-auto px-2 py-1 bg-[var(--accent)] text-white" onClick={save} disabled={loading}>Save</button>
      </div>
      {error && <div className="text-sm text-red-400">{error}</div>}

      <div className="border border-neutral-700 bg-[var(--panel-2)] p-3 text-sm space-y-2">
        <div className="flex items-center gap-2">
          <label className="inline-flex items-center gap-2"><input type="checkbox" className="accent-[var(--accent)]" checked={!!cfg.https_enabled} onChange={(e) => setCfg({ ...cfg, https_enabled: e.target.checked })} /> HTTPS enabled</label>
          <label className="inline-flex items-center gap-2"><input type="checkbox" className="accent-[var(--accent)]" checked={!!cfg.redirect_http_to_https} onChange={(e) => setCfg({ ...cfg, redirect_http_to_https: e.target.checked })} /> Redirect HTTP → HTTPS</label>
        </div>
        <div className="grid grid-cols-1 gap-2">
          {cfg.services.map((s, idx) => (
            <div key={idx} className="grid grid-cols-6 gap-2 items-center">
              <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={s.name} onChange={(e) => { const n=[...cfg.services]; n[idx]={...n[idx], name:e.target.value}; setCfg({...cfg, services:n}) }} />
              <select className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={s.protocol||'tcp'} onChange={(e)=>{const n=[...cfg.services]; n[idx]={...n[idx], protocol:e.target.value as any}; setCfg({...cfg, services:n})}}>
                <option value="tcp">tcp</option>
                <option value="udp">udp</option>
                <option value="any">any</option>
              </select>
              <input type="number" className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={s.internal_port} onChange={(e) => { const n=[...cfg.services]; n[idx]={...n[idx], internal_port:Number(e.target.value)}; setCfg({...cfg, services:n}) }} />
              <input type="number" className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" placeholder="external" value={s.external_port ?? ''} onChange={(e) => { const n=[...cfg.services]; n[idx]={...n[idx], external_port:e.target.value ? Number(e.target.value) : null}; setCfg({...cfg, services:n}) }} />
              <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={() => { const n=[...cfg.services]; n.splice(idx,1); setCfg({...cfg, services:n}) }}>Remove</button>
            </div>
          ))}
          <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={() => setCfg({ ...cfg, services: [...cfg.services, { key: 'custom', name: 'Custom', internal_port: 0, external_port: null, protocol: 'tcp' }] })}>Add Service</button>
        </div>
      </div>
    </div>
  )
}


