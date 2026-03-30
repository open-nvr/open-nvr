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
import { useNavigate } from 'react-router-dom'
import { apiService } from '../../lib/apiService'

type Camera = {
  id: number
  name: string
  rtsp_url?: string | null
}

type CameraConfig = {
  id: number
  camera_id: number
  stream_protocol: 'rtsp' | 'rtmp' | 'webrtc'
  source_url?: string | null
  recording_enabled: boolean
  recording_path?: string | null
  recording_segment_seconds: number
  webrtc_publisher: boolean
  rtmp_publisher: boolean
  rtsp_transport?: 'udp' | 'tcp' | 'auto' | null
  extra_options?: string | null
  // Proxy fields removed
}

export function CameraConfigManager() {
  const navigate = useNavigate()
  const [cameras, setCameras] = useState<Camera[]>([])
  const [selectedCamId, setSelectedCamId] = useState<number | ''>('')
  const [cfg, setCfg] = useState<Partial<CameraConfig>>({ stream_protocol: 'rtsp', recording_enabled: false, recording_segment_seconds: 60 })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [limit, setLimit] = useState(20)
  const skip = useMemo(() => (page - 1) * limit, [page, limit])
  const [total, setTotal] = useState(0)

  useEffect(() => {
    ;(async () => {
      try {
        setLoading(true)
        setError(null)
        const { data } = await apiService.getCameras({ skip, limit, active_only: true, q: q || undefined })
        setCameras(data.cameras.map((c: any) => ({ id: c.id, name: c.name, rtsp_url: c.rtsp_url })))
        setTotal(data.total ?? 0)
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load cameras')
      } finally {
        setLoading(false)
      }
    })()
  }, [skip, limit, q])

  const loadConfig = async (camId: number) => {
    try {
      setLoading(true)
      setError(null)
      setNotice(null)
      const { data } = await apiService.getCameraConfig(camId)
      
      // Auto-populate source URL if it's empty and camera has RTSP URL
      const selectedCamera = cameras.find(c => c.id === camId)
      if (!data.source_url && selectedCamera?.rtsp_url) {
        data.source_url = selectedCamera.rtsp_url
      }
      
      setCfg(data)
    } catch (e: any) {
      if (e?.status === 404) {
        // config not found: reset to defaults but keep camera_id
        // Auto-populate source URL if camera has RTSP URL
        const selectedCamera = cameras.find(c => c.id === camId)
        setCfg({ 
          camera_id: camId, 
          stream_protocol: 'rtsp', 
          recording_enabled: false, 
          recording_segment_seconds: 60,
          source_url: selectedCamera?.rtsp_url || null
        })
      } else {
        setError(e?.data?.detail || e?.message || 'Failed to load config')
      }
    } finally {
      setLoading(false)
    }
  }

  const onSelectCamera = async (id: number) => {
    setSelectedCamId(id)
    await loadConfig(id)
  }

  const onCreateOrUpdate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedCamId) return
    try {
      setLoading(true)
      setError(null)
      setNotice(null)
      if ((cfg as any).id) {
        await apiService.updateCameraConfig(selectedCamId, {
          stream_protocol: cfg.stream_protocol,
          source_url: cfg.source_url || null,
          recording_enabled: !!cfg.recording_enabled,
          recording_path: cfg.recording_path || null,
          recording_segment_seconds: Number(cfg.recording_segment_seconds) || 60,
          webrtc_publisher: !!cfg.webrtc_publisher,
          rtmp_publisher: !!cfg.rtmp_publisher,
          rtsp_transport: cfg.rtsp_transport || null,
          extra_options: cfg.extra_options || null,
          // proxy fields removed
        })
        setNotice('Configuration updated')
      } else {
        await apiService.createCameraConfig({
          camera_id: selectedCamId,
          stream_protocol: (cfg.stream_protocol as any) || 'rtsp',
          source_url: cfg.source_url || null,
          recording_enabled: !!cfg.recording_enabled,
          recording_path: cfg.recording_path || null,
          recording_segment_seconds: Number(cfg.recording_segment_seconds) || 60,
          webrtc_publisher: !!cfg.webrtc_publisher,
          rtmp_publisher: !!cfg.rtmp_publisher,
          rtsp_transport: cfg.rtsp_transport || null,
          extra_options: cfg.extra_options || null,
          // proxy fields removed
        })
        setNotice('Configuration created')
      }
      await loadConfig(selectedCamId)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save config')
    } finally {
      setLoading(false)
    }
  }

  const onProvision = async () => {
    if (!selectedCamId) return
    try {
      setLoading(true)
      setError(null)
      setNotice(null)
      const { data } = await apiService.provisionCameraPath(selectedCamId)
      setNotice(`Provisioned: ${data?.status || ''}`)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Provision failed')
    } finally {
      setLoading(false)
    }
  }

  const onUnprovision = async () => {
    if (!selectedCamId) return
    try {
      setLoading(true)
      setError(null)
      setNotice(null)
      const { data } = await apiService.unprovisionCameraPath(selectedCamId)
      setNotice(`Unprovisioned: ${data?.status || ''}`)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Unprovision failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">Camera Config</h2>
        <div className="ml-auto flex items-center gap-2 text-sm">
          <input className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1" placeholder="Search cameras" value={q} onChange={(e) => { setPage(1); setQ(e.target.value) }} />
          <select className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1" value={limit} onChange={(e) => { setPage(1); setLimit(Number(e.target.value)) }}>
            {[10, 20, 50].map(n => <option key={n} value={n}>{n}/page</option>)}
          </select>
        </div>
      </div>

      {error && <div className="text-sm text-red-400">{error}</div>}
      {notice && <div className="text-sm text-emerald-400">{notice}</div>}

      <div className="grid grid-cols-3 gap-4">
        <div className="col-span-1 border border-neutral-700">
          <table className="w-full text-sm">
            <thead className="bg-[var(--panel-2)] text-left">
              <tr>
                <th className="p-2">Camera</th>
              </tr>
            </thead>
            <tbody>
              {cameras.map(c => (
                <tr key={c.id} className={`odd:bg-[var(--bg-2)] even:bg-[var(--panel)] ${selectedCamId === c.id ? 'outline outline-1 outline-[var(--accent)]' : ''}`}>
                  <td className="p-2">
                    <button className="text-left w-full" onClick={() => onSelectCamera(c.id)}>{c.name}</button>
                  </td>
                </tr>
              ))}
              {cameras.length === 0 && (
                <tr>
                  <td className="p-2 text-[var(--text-dim)]">No cameras</td>
                </tr>
              )}
            </tbody>
          </table>
          <div className="flex items-center gap-2 p-2 text-sm border-t border-neutral-700">
            <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" disabled={page <= 1} onClick={() => setPage(p => Math.max(1, p - 1))}>Prev</button>
            <span>Page {page}</span>
            <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" disabled={cameras.length < limit} onClick={() => setPage(p => p + 1)}>Next</button>
          </div>
        </div>

        <div className="col-span-2 border border-neutral-700 p-3 text-sm">
          {!selectedCamId ? (
            <div className="text-[var(--text-dim)]">Select a camera to configure.</div>
          ) : (
            <form className="grid grid-cols-2 gap-3" onSubmit={onCreateOrUpdate}>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Stream Protocol</span>
                <select className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.stream_protocol as any} onChange={(e) => setCfg({ ...cfg, stream_protocol: e.target.value as any })}>
                  <option value="rtsp">RTSP</option>
                  <option value="rtmp">RTMP</option>
                  <option value="webrtc">WebRTC</option>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">RTSP Transport</span>
                <select className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={(cfg.rtsp_transport as any) || ''} onChange={(e) => setCfg({ ...cfg, rtsp_transport: (e.target.value || null) as any })}>
                  <option value="">(auto)</option>
                  <option value="udp">udp</option>
                  <option value="tcp">tcp</option>
                  <option value="auto">auto</option>
                </select>
              </label>
              <label className="flex flex-col gap-1 col-span-2">
                <span className="text-[var(--text-dim)]">Source URL</span>
                <input className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.source_url || ''} onChange={(e) => setCfg({ ...cfg, source_url: e.target.value })} placeholder="rtsp:// or webrtc:// or rtmp://" />
              </label>
              
              {/* Recording Configuration */}
              <div className="col-span-2 border-t border-neutral-600 pt-3 mt-2">
                <h4 className="text-[var(--text-dim)] font-medium mb-2">Recording Settings</h4>
                <div className="grid grid-cols-2 gap-3">
                  <label className="flex items-center gap-2">
                    <input 
                      type="checkbox" 
                      className="accent-[var(--accent)]" 
                      checked={!!cfg.recording_enabled} 
                      onChange={(e) => setCfg({ ...cfg, recording_enabled: e.target.checked })} 
                    />
                    <span>Enable Recording</span>
                  </label>
                  
                  {cfg.recording_enabled && (
                    <>
                      <label className="flex flex-col gap-1">
                        <span className="text-[var(--text-dim)]">Segment Duration</span>
                        <select 
                          className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" 
                          value={cfg.recording_segment_seconds || 60} 
                          onChange={(e) => setCfg({ ...cfg, recording_segment_seconds: Number(e.target.value) })}
                        >
                          <option value="60">1 minute</option>
                          <option value="300">5 minutes</option>
                          <option value="600">10 minutes</option>
                          <option value="1800">30 minutes</option>
                          <option value="3600">1 hour</option>
                        </select>
                      </label>
                      
                      <label className="flex flex-col gap-1 col-span-2">
                        <span className="text-[var(--text-dim)]">Recording Path (optional)</span>
                        <input 
                          className="bg-[var(--panel)] border border-neutral-700 px-2 py-1" 
                          value={cfg.recording_path || ''} 
                          onChange={(e) => setCfg({ ...cfg, recording_path: e.target.value || null })} 
                          placeholder="Leave empty for default path"
                        />
                      </label>
                    </>
                  )}
                </div>
              </div>
              
              {/* Publisher Settings */}
              <div className="col-span-2 border-t border-neutral-600 pt-3 mt-2">
                <h4 className="text-[var(--text-dim)] font-medium mb-2">Publisher Settings</h4>
                <div className="grid grid-cols-2 gap-3">
                  <label className="flex items-center gap-2">
                    <input type="checkbox" className="accent-[var(--accent)]" checked={!!cfg.webrtc_publisher} onChange={(e) => setCfg({ ...cfg, webrtc_publisher: e.target.checked })} /> 
                    <span>WebRTC publisher</span>
                  </label>
                  <label className="flex items-center gap-2">
                    <input type="checkbox" className="accent-[var(--accent)]" checked={!!cfg.rtmp_publisher} onChange={(e) => setCfg({ ...cfg, rtmp_publisher: e.target.checked })} /> 
                    <span>RTMP publisher</span>
                  </label>
                </div>
              </div>

              {/* RTSP Proxy controls removed */}

              <div className="col-span-2 flex items-center gap-2 mt-2">
                <button className="px-3 py-1 bg-[var(--accent)] text-white" disabled={loading}>{(cfg as any).id ? 'Update' : 'Create'}</button>
                <button type="button" className="px-3 py-1 bg-[var(--panel)] border border-neutral-700" onClick={onProvision} disabled={loading}>Provision</button>
                <button type="button" className="px-3 py-1 bg-[var(--panel)] border border-neutral-700" onClick={onUnprovision} disabled={loading}>Unprovision</button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  )
}

// ProxyControls removed
