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

type NatConfig = {
  public_ip?: string
  stun_servers?: string[]
  turn_servers?: { url: string; username?: string; credential?: string }[]
  upnp_enabled?: boolean
  mappings?: { service: string; internal_port: number; external_port?: number | null; protocol?: 'tcp'|'udp'|'any'; status?: 'open'|'closed' }[]
}

export function SecurityNAT() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cfg, setCfg] = useState<NatConfig>({ stun_servers: [], turn_servers: [], mappings: [] })

  const canAdmin = !!me?.is_superuser

  const load = async () => {
    try {
      setLoading(true)
      setError(null)
      const res = await apiService.getSecuritySetting('nat')
      const v = (res.data && (res.data as any).value) ? (res.data as any).value : {}
      setCfg({
        public_ip: v.public_ip || '',
        stun_servers: v.stun_servers || [],
        turn_servers: v.turn_servers || [],
        upnp_enabled: v.upnp_enabled || false,
        mappings: v.mappings || [],
      })
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load NAT config')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { if (canAdmin) load() }, [canAdmin])

  const save = async () => {
    try {
      setLoading(true)
      setError(null)
      await apiService.setSecuritySetting('nat', cfg as any)
      await load()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save NAT config')
    } finally {
      setLoading(false)
    }
  }

  if (!canAdmin) return <div className="text-sm text-amber-400">Admin only.</div>

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">NAT</h2>
        <button className="ml-auto px-2 py-1 bg-[var(--accent)] text-white" onClick={save} disabled={loading}>Save</button>
      </div>
      {error && <div className="text-sm text-red-400">{error}</div>}

      <div className="grid grid-cols-2 gap-3 text-sm">
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Detected public IP</span>
          <input className="w-56 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.public_ip || ''} onChange={(e)=>setCfg({...cfg, public_ip: e.target.value})} />
        </label>
        <label className="flex items-center justify-between gap-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <span>Enable UPnP/NAT-PMP</span>
          <input type="checkbox" className="accent-[var(--accent)]" checked={!!cfg.upnp_enabled} onChange={(e)=>setCfg({...cfg, upnp_enabled: e.target.checked})} />
        </label>
        <div className="border border-neutral-700 bg-[var(--panel-2)] p-2">
          <div className="text-[var(--text-dim)] mb-1">STUN servers</div>
          {(cfg.stun_servers || []).map((u, i) => (
            <div key={i} className="flex items-center gap-2 mb-1">
              <input className="flex-1 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={u} onChange={(e)=>{const n=[...(cfg.stun_servers||[])]; n[i]=e.target.value; setCfg({...cfg, stun_servers:n})}} />
              <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={()=>{const n=[...(cfg.stun_servers||[])]; n.splice(i,1); setCfg({...cfg, stun_servers:n})}}>Remove</button>
            </div>
          ))}
          <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={()=>setCfg({...cfg, stun_servers:[...(cfg.stun_servers||[]), 'stun:stun.l.google.com:19302']})}>Add STUN</button>
        </div>
        <div className="border border-neutral-700 bg-[var(--panel-2)] p-2 space-y-1">
          <div className="text-[var(--text-dim)]">TURN servers</div>
          {(cfg.turn_servers || []).map((t, i) => (
            <div key={i} className="grid grid-cols-4 gap-2 items-center">
              <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" placeholder="turn:example:3478" value={t.url} onChange={(e)=>{const n=[...(cfg.turn_servers||[])]; n[i]={...n[i], url:e.target.value}; setCfg({...cfg, turn_servers:n})}} />
              <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" placeholder="username" value={t.username||''} onChange={(e)=>{const n=[...(cfg.turn_servers||[])]; n[i]={...n[i], username:e.target.value}; setCfg({...cfg, turn_servers:n})}} />
              <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" placeholder="credential" value={t.credential||''} onChange={(e)=>{const n=[...(cfg.turn_servers||[])]; n[i]={...n[i], credential:e.target.value}; setCfg({...cfg, turn_servers:n})}} />
              <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={()=>{const n=[...(cfg.turn_servers||[])]; n.splice(i,1); setCfg({...cfg, turn_servers:n})}}>Remove</button>
            </div>
          ))}
          <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={()=>setCfg({...cfg, turn_servers:[...(cfg.turn_servers||[]), { url:'turn:example:3478', username:'', credential:'' }]})}>Add TURN</button>
        </div>

        <div className="col-span-2 border border-neutral-700 bg-[var(--panel-2)] p-2">
          <div className="text-[var(--text-dim)] mb-1">Port Mappings</div>
          <div className="space-y-1">
            {(cfg.mappings || []).map((m, i) => (
              <div key={i} className="grid grid-cols-6 gap-2 items-center">
                <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={m.service} onChange={(e)=>{const n=[...(cfg.mappings||[])]; n[i]={...n[i], service:e.target.value}; setCfg({...cfg, mappings:n})}} />
                <select className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={m.protocol||'tcp'} onChange={(e)=>{const n=[...(cfg.mappings||[])]; n[i]={...n[i], protocol:e.target.value as any}; setCfg({...cfg, mappings:n})}}>
                  <option value="tcp">tcp</option>
                  <option value="udp">udp</option>
                  <option value="any">any</option>
                </select>
                <input type="number" className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={m.internal_port} onChange={(e)=>{const n=[...(cfg.mappings||[])]; n[i]={...n[i], internal_port:Number(e.target.value)}; setCfg({...cfg, mappings:n})}} />
                <input type="number" className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={m.external_port ?? ''} onChange={(e)=>{const n=[...(cfg.mappings||[])]; n[i]={...n[i], external_port:e.target.value?Number(e.target.value):null}; setCfg({...cfg, mappings:n})}} />
                <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={m.status || ''} onChange={(e)=>{const n=[...(cfg.mappings||[])]; n[i]={...n[i], status:e.target.value as any}; setCfg({...cfg, mappings:n})}} />
                <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={()=>{const n=[...(cfg.mappings||[])]; n.splice(i,1); setCfg({...cfg, mappings:n})}}>Remove</button>
              </div>
            ))}
            <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" onClick={()=>setCfg({...cfg, mappings:[...(cfg.mappings||[]), { service:'Custom', internal_port:0, external_port:null, protocol:'tcp', status:'closed' }]})}>Add Mapping</button>
          </div>
        </div>
      </div>
    </div>
  )
}


