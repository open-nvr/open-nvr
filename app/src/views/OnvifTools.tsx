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
import { apiService } from '../lib/apiService'

type Device = { ip: string | null; service_urls: string[] }
type Profile = { token: string; name: string }

export function OnvifTools() {
  const [loading, setLoading] = useState(false)
  const [devices, setDevices] = useState<Device[]>([])
  const [selectedIp, setSelectedIp] = useState<string>('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [port, setPort] = useState<number>(80)
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [profileToken, setProfileToken] = useState('')
  const [streamUri, setStreamUri] = useState('')
  const [err, setErr] = useState<string>('')

  const canAuth = selectedIp && username && password

  const discover = async () => {
    setLoading(true)
    setErr('')
    try {
      const res = await apiService.onvifDiscover()
      setDevices(res.data.devices || [])
      // Select first ONVIF-looking device automatically
      const first = (res.data.devices || []).find((d: Device) => (d.service_urls || []).some((u) => u.toLowerCase().includes('/onvif')))
      if (first?.ip) setSelectedIp(first.ip)
    } catch (e: any) {
      setErr(e?.data?.detail || e.message || 'Discovery failed')
    } finally {
      setLoading(false)
    }
  }

  const loadProfiles = async () => {
    if (!canAuth) return
    setLoading(true)
    setErr('')
    try {
      const res = await apiService.onvifProfiles(selectedIp, { username, password, port })
      setProfiles(res.data.profiles || [])
      if ((res.data.profiles || []).length) setProfileToken(res.data.profiles[0].token)
    } catch (e: any) {
      setProfiles([])
      setErr(e?.data?.detail || e.message || 'GetProfiles failed')
    } finally {
      setLoading(false)
    }
  }

  const loadStreamUri = async () => {
    if (!canAuth || !profileToken) return
    setLoading(true)
    setErr('')
    try {
      const res = await apiService.onvifStreamUri(selectedIp, { username, password, profileToken, port })
      setStreamUri(res.data.uri)
    } catch (e: any) {
      setStreamUri('')
      setErr(e?.data?.detail || e.message || 'GetStreamUri failed')
    } finally {
      setLoading(false)
    }
  }

  const ptzMove = async (x=0, y=0, z=0) => {
    if (!canAuth || !profileToken) return
    setLoading(true)
    setErr('')
    try {
      await apiService.onvifPtzMove(selectedIp, { username, password, profileToken, x, y, z, port })
    } catch (e: any) {
      setErr(e?.data?.detail || e.message || 'PTZ move failed')
    } finally {
      setLoading(false)
    }
  }

  const ptzStop = async () => {
    if (!canAuth || !profileToken) return
    setLoading(true)
    setErr('')
    try {
      await apiService.onvifPtzStop(selectedIp, { username, password, profileToken, port })
    } catch (e: any) {
      setErr(e?.data?.detail || e.message || 'PTZ stop failed')
    } finally {
      setLoading(false)
    }
  }

  const addCamera = async () => {
    if (!canAuth || !selectedIp) return
    setLoading(true)
    setErr('')
    try {
      // Ensure we have an RTSP URL if possible
      let rtspUrl = streamUri
      if (!rtspUrl && profileToken) {
        try {
          const res = await apiService.onvifStreamUri(selectedIp, { username, password, profileToken, port })
          rtspUrl = res.data.uri
        } catch {}
      }
      // Derive RTSP port if present in URI, otherwise default to 554
      let cameraPort = 554
      try {
        if (rtspUrl) {
          const u = new URL(rtspUrl)
          cameraPort = parseInt(u.port || '554') || 554
        }
      } catch {}
      const payload: any = {
        name: `Camera ${selectedIp}`,
        description: null,
        ip_address: selectedIp,
        port: cameraPort,
        username,
        password,
        rtsp_url: rtspUrl || null,
        location: null,
        vlan: null,
        status: 'unknown',
      }
      await apiService.createCamera(payload)
      alert('Camera added successfully')
    } catch (e: any) {
      setErr(e?.data?.detail || e.message || 'Failed to add camera')
    } finally {
      setLoading(false)
    }
  }

  const getPresets = async () => {
    if (!canAuth || !profileToken) return
    setLoading(true)
    setErr('')
    try {
      const res = await apiService.onvifPreset(selectedIp, { username, password, profileToken, action: 'getPresets', port })
      alert(JSON.stringify(res.data.result?.presets || [], null, 2))
    } catch (e: any) {
      setErr(e?.data?.detail || e.message || 'GetPresets failed')
    } finally {
      setLoading(false)
    }
  }

  const setPreset = async () => {
    const name = prompt('Preset name?') || undefined
    if (!name) return
    if (!canAuth || !profileToken) return
    setLoading(true)
    setErr('')
    try {
      const res = await apiService.onvifPreset(selectedIp, { username, password, profileToken, action: 'setPreset', name, port })
      alert('SetPreset OK. Token: ' + (res.data.result?.preset_token || 'unknown'))
    } catch (e: any) {
      setErr(e?.data?.detail || e.message || 'SetPreset failed')
    } finally {
      setLoading(false)
    }
  }

  const gotoPreset = async () => {
    const p = prompt('Preset token?') || undefined
    if (!p) return
    if (!canAuth || !profileToken) return
    setLoading(true)
    setErr('')
    try {
      await apiService.onvifPreset(selectedIp, { username, password, profileToken, action: 'gotoPreset', presetToken: p, port })
      alert('Moving to preset...')
    } catch (e: any) {
      setErr(e?.data?.detail || e.message || 'GotoPreset failed')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    // auto discover on load
    discover()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">ONVIF Tools</h1>
      {err && <div className="text-red-400 text-sm">{err}</div>}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-[var(--bg-2)] border border-[var(--border)] p-3 rounded">
          <div className="flex items-center justify-between">
            <h2 className="font-medium">Discovery</h2>
            <button className="text-sm px-2 py-1 bg-[var(--panel)] rounded" onClick={discover} disabled={loading}>Refresh</button>
          </div>
          <div className="mt-2 space-y-2 max-h-60 overflow-auto">
            {devices.map((d, i) => (
              <div key={i} className={`p-2 rounded cursor-pointer ${selectedIp===d.ip ? 'bg-[var(--panel-2)]' : 'hover:bg-[var(--panel-2)]'}`} onClick={() => d.ip && setSelectedIp(d.ip)}>
                <div className="text-sm">IP: <span className="font-mono">{d.ip || 'unknown'}</span></div>
                <div className="text-xs text-[var(--text-dim)] break-all">{d.service_urls?.join(', ')}</div>
              </div>
            ))}
          </div>
        </div>

        <div className="bg-[var(--bg-2)] border border-[var(--border)] p-3 rounded">
          <h2 className="font-medium">Auth & Profiles</h2>
          <div className="mt-2 grid grid-cols-2 gap-2">
            <label className="text-xs">IP
              <input className="w-full mt-1 px-2 py-1 bg-[var(--panel)] border border-[var(--border)] rounded" value={selectedIp} onChange={(e)=>setSelectedIp(e.target.value)} />
            </label>
            <label className="text-xs">Port
              <input type="number" className="w-full mt-1 px-2 py-1 bg-[var(--panel)] border border-[var(--border)] rounded" value={port} onChange={(e)=>setPort(parseInt(e.target.value)||80)} />
            </label>
            <label className="text-xs">Username
              <input className="w-full mt-1 px-2 py-1 bg-[var(--panel)] border border-[var(--border)] rounded" value={username} onChange={(e)=>setUsername(e.target.value)} />
            </label>
            <label className="text-xs">Password
              <input type="password" className="w-full mt-1 px-2 py-1 bg-[var(--panel)] border border-[var(--border)] rounded" value={password} onChange={(e)=>setPassword(e.target.value)} />
            </label>
          </div>
          <div className="mt-2 flex gap-2">
            <button className="text-sm px-2 py-1 bg-[var(--panel)] rounded" onClick={loadProfiles} disabled={loading || !canAuth}>Load Profiles</button>
            <select className="text-sm px-2 py-1 bg-[var(--panel)] border border-[var(--border)] rounded" value={profileToken} onChange={(e)=>setProfileToken(e.target.value)}>
              {profiles.map((p)=> <option key={p.token} value={p.token}>{p.name || p.token}</option>)}
            </select>
          </div>
          <div className="mt-2 flex gap-2">
            <button className="btn" onClick={loadStreamUri} disabled={loading || !profileToken}>Get Stream URI</button>
            {streamUri && <code className="text-xs break-all">{streamUri}</code>}
          </div>
          <div className="mt-2">
            <button className="btn btn-primary disabled:opacity-50" onClick={addCamera} disabled={loading || !canAuth}>➕ Add Camera</button>
          </div>
        </div>
      </div>

      <div className="bg-[var(--bg-2)] border border-[var(--border)] p-3 rounded">
        <h2 className="font-medium">PTZ</h2>
        <div className="text-xs text-[var(--text-dim)]">Hold buttons to keep moving; single click sends one continuous move request.</div>
        <div className="mt-2 grid grid-cols-3 gap-2 max-w-md">
          <button className="px-2 py-2 bg-[var(--panel)] rounded" onMouseDown={()=>ptzMove(-0.5,0,0)} onMouseUp={ptzStop}>◀️ Pan Left</button>
          <button className="px-2 py-2 bg-[var(--panel)] rounded" onMouseDown={()=>ptzMove(0,0.5,0)} onMouseUp={ptzStop}>🔼 Tilt Up</button>
          <button className="px-2 py-2 bg-[var(--panel)] rounded" onMouseDown={()=>ptzMove(0.5,0,0)} onMouseUp={ptzStop}>▶️ Pan Right</button>
          <div />
          <button className="px-2 py-2 bg-[var(--panel)] rounded" onMouseDown={()=>ptzMove(0,-0.5,0)} onMouseUp={ptzStop}>🔽 Tilt Down</button>
          <div />
          <button className="px-2 py-2 bg-[var(--panel)] rounded" onMouseDown={()=>ptzMove(0,0,0.3)} onMouseUp={ptzStop}>➕ Zoom In</button>
          <button className="px-2 py-2 bg-[var(--panel)] rounded" onMouseDown={()=>ptzMove(0,0,-0.3)} onMouseUp={ptzStop}>➖ Zoom Out</button>
          <button className="px-2 py-2 bg-[var(--panel)] rounded" onClick={ptzStop}>⏹ Stop</button>
        </div>
        <div className="mt-3 flex gap-2">
          <button className="text-sm px-2 py-1 bg-[var(--panel)] rounded" onClick={getPresets}>Get Presets</button>
          <button className="text-sm px-2 py-1 bg-[var(--panel)] rounded" onClick={setPreset}>Set Preset</button>
          <button className="text-sm px-2 py-1 bg-[var(--panel)] rounded" onClick={gotoPreset}>Goto Preset</button>
        </div>
      </div>
    </div>
  )
}
