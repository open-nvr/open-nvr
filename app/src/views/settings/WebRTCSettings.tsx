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

type TurnServer = { url: string; username?: string; credential?: string }

type Settings = {
  stun_servers: string[]
  turn_servers: TurnServer[]
  ice: { transport_policy: 'all' | 'relay'; candidate_pool_size: number; trickle: boolean }
  bandwidth: { video_max_bitrate_kbps: number; audio_max_bitrate_kbps: number; max_fps: number; resolution_cap: { width: number; height: number } }
  codecs: { video_preferred: string[]; audio_preferred: string[] }
}

export function WebRTCSettings() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cfg, setCfg] = useState<Settings>({
    stun_servers: [],
    turn_servers: [],
    ice: { transport_policy: 'all', candidate_pool_size: 0, trickle: true },
    bandwidth: { video_max_bitrate_kbps: 2500, audio_max_bitrate_kbps: 64, max_fps: 30, resolution_cap: { width: 1920, height: 1080 } },
    codecs: { video_preferred: ['h264','vp9','vp8','av1'], audio_preferred: ['opus'] },
  })

  const canAdmin = !!me?.is_superuser

  const load = async () => {
    try {
      setLoading(true)
      setError(null)
      const res = await apiService.getWebRTCSettings()
      setCfg(res.data as Settings)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load WebRTC settings')
    } finally {
      setLoading(false)
    }
  }

  // Load when component mounts or when admin state becomes available
  useEffect(() => { if (canAdmin) load() }, [canAdmin])

  const save = async () => {
    try {
      setLoading(true)
      setError(null)
      await apiService.updateWebRTCSettings(cfg as any)
      await load()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save WebRTC settings')
    } finally {
      setLoading(false)
    }
  }

  if (!canAdmin) return <div className="text-sm text-amber-400">Admin only.</div>

  const listEdit = (label: string, items: string[], onChange: (arr: string[]) => void) => (
    <div className="border border-[var(--border)] bg-[var(--panel-2)] p-2">
      <div className="text-[var(--text-dim)] mb-1">{label}</div>
      <div className="space-y-1">
        {items.map((it, i) => (
          <div key={i} className="flex items-center gap-2">
            <input className="flex-1 input" value={it} onChange={(e)=>{const n=[...items]; n[i]=e.target.value; onChange(n)}} />
            <button className="btn" onClick={()=>{const n=[...items]; n.splice(i,1); onChange(n)}}>Remove</button>
          </div>
        ))}
        <button className="btn" onClick={()=>onChange([...items, ''])}>Add</button>
      </div>
    </div>
  )

  const codecListEdit = (label: string, items: string[], onChange: (arr: string[]) => void) => (
    <div className="border border-[var(--border)] bg-[var(--panel-2)] p-2">
      <div className="text-[var(--text-dim)] mb-1">{label} (drag to reorder not implemented; top is most preferred)</div>
      <div className="space-y-1">
        {items.map((it, i) => (
          <div key={i} className="flex items-center gap-2">
            <input className="flex-1 input" value={it} onChange={(e)=>{const n=[...items]; n[i]=e.target.value; onChange(n)}} />
            <button className="btn" onClick={()=>{const n=[...items]; n.splice(i,1); onChange(n)}}>Remove</button>
          </div>
        ))}
        <button className="btn" onClick={()=>onChange([...items, ''])}>Add</button>
      </div>
    </div>
  )

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">WebRTC Settings</h2>
  <button className="ml-auto btn btn-primary" onClick={save} disabled={loading}>Save</button>
      </div>
      {error && <div className="text-sm text-red-400">{error}</div>}

      <div className="grid grid-cols-2 gap-3 text-sm">
        {listEdit('STUN Servers', cfg.stun_servers || [], (arr)=>setCfg({...cfg, stun_servers: arr}))}

  <div className="border border-[var(--border)] bg-[var(--panel-2)] p-2 space-y-1">
          <div className="text-[var(--text-dim)]">TURN Servers</div>
          {(cfg.turn_servers || []).map((t, i) => (
            <div key={i} className="grid grid-cols-4 gap-2 items-center">
              <input className="input" placeholder="turn:example:3478" value={t.url} onChange={(e)=>{const n=[...(cfg.turn_servers||[])]; n[i]={...n[i], url:e.target.value}; setCfg({...cfg, turn_servers:n})}} />
              <input className="input" placeholder="username" value={t.username||''} onChange={(e)=>{const n=[...(cfg.turn_servers||[])]; n[i]={...n[i], username:e.target.value}; setCfg({...cfg, turn_servers:n})}} />
              <input className="input" placeholder="credential" value={t.credential||''} onChange={(e)=>{const n=[...(cfg.turn_servers||[])]; n[i]={...n[i], credential:e.target.value}; setCfg({...cfg, turn_servers:n})}} />
              <button className="btn" onClick={()=>{const n=[...(cfg.turn_servers||[])]; n.splice(i,1); setCfg({...cfg, turn_servers:n})}}>Remove</button>
            </div>
          ))}
          <button className="btn" onClick={()=>setCfg({...cfg, turn_servers:[...(cfg.turn_servers||[]), { url:'turn:example:3478', username:'', credential:'' }]})}>Add TURN</button>
        </div>

        <div className="border border-[var(--border)] bg-[var(--panel-2)] p-2 space-y-2">
          <div className="text-[var(--text-dim)]">ICE</div>
          <label className="flex items-center justify-between gap-2">
            <span>Transport policy</span>
            <select className="w-32 select" value={cfg.ice.transport_policy} onChange={(e)=>setCfg({...cfg, ice:{...cfg.ice, transport_policy: e.target.value as any}})}>
              <option value="all">all</option>
              <option value="relay">relay</option>
            </select>
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Candidate pool size</span>
            <input type="number" min={0} max={10} className="w-24 input" value={cfg.ice.candidate_pool_size} onChange={(e)=>setCfg({...cfg, ice:{...cfg.ice, candidate_pool_size:Number(e.target.value)}})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Enable trickle ICE</span>
            <input type="checkbox" className="accent-[var(--accent)]" checked={cfg.ice.trickle} onChange={(e)=>setCfg({...cfg, ice:{...cfg.ice, trickle:e.target.checked}})} />
          </label>
        </div>

  <div className="border border-[var(--border)] bg-[var(--panel-2)] p-2 space-y-2">
          <div className="text-[var(--text-dim)]">Bandwidth</div>
          <label className="flex items-center justify-between gap-2">
            <span>Video max bitrate (kbps)</span>
            <input type="number" min={100} max={100000} className="w-28 input" value={cfg.bandwidth.video_max_bitrate_kbps} onChange={(e)=>setCfg({...cfg, bandwidth:{...cfg.bandwidth, video_max_bitrate_kbps:Number(e.target.value)}})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Audio max bitrate (kbps)</span>
            <input type="number" min={16} max={512} className="w-28 input" value={cfg.bandwidth.audio_max_bitrate_kbps} onChange={(e)=>setCfg({...cfg, bandwidth:{...cfg.bandwidth, audio_max_bitrate_kbps:Number(e.target.value)}})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Max FPS</span>
            <input type="number" min={1} max={120} className="w-24 input" value={cfg.bandwidth.max_fps} onChange={(e)=>setCfg({...cfg, bandwidth:{...cfg.bandwidth, max_fps:Number(e.target.value)}})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Resolution cap</span>
            <div className="flex items-center gap-2">
              <input type="number" min={160} max={7680} className="w-24 input" value={cfg.bandwidth.resolution_cap.width} onChange={(e)=>setCfg({...cfg, bandwidth:{...cfg.bandwidth, resolution_cap:{...cfg.bandwidth.resolution_cap, width:Number(e.target.value)}}})} />
              <span>×</span>
              <input type="number" min={120} max={4320} className="w-24 input" value={cfg.bandwidth.resolution_cap.height} onChange={(e)=>setCfg({...cfg, bandwidth:{...cfg.bandwidth, resolution_cap:{...cfg.bandwidth.resolution_cap, height:Number(e.target.value)}}})} />
            </div>
          </label>
        </div>

        {codecListEdit('Preferred Video Codecs', cfg.codecs.video_preferred || [], (arr)=>setCfg({...cfg, codecs:{...cfg.codecs, video_preferred: arr}}))}
        {codecListEdit('Preferred Audio Codecs', cfg.codecs.audio_preferred || [], (arr)=>setCfg({...cfg, codecs:{...cfg.codecs, audio_preferred: arr}}))}
      </div>
    </div>
  )
}


