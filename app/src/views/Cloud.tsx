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
import { apiService } from '../lib/apiService'
import { Play, Square, Server, Plus, Trash2, AlertCircle, CheckCircle, Loader2, Info, X, Shield, Pencil } from 'lucide-react'

type CloudSettings = {
  streaming: {
    enabled: boolean
    server_url: string
    auth_token?: string
    protocol: 'webrtc' | 'rtmp' | 'hls'
    video_codec: 'h264' | 'h265' | 'vp9' | 'av1'
    encryption: 'none' | 'dtls-srtp' | 'aes-128' | 'sample-aes'
  }
  recording: {
    enabled: boolean
    use_byok: boolean
    server_url: string
    bucket?: string
    access_key?: string
    secret_key?: string
    region?: string
    storage_class?: string
  }
}

type Camera = {
  id: number
  name: string
  ip_address: string
}

type StreamTarget = {
  target_id: string
  camera_id: number
  camera_name: string | null
  enabled: boolean
  server_url: string
  stream_key_set: boolean
  protocol: string
  use_tls: boolean
  use_custom_ca: boolean
  video_codec: string
  audio_codec: string
  video_bitrate: string | null
  audio_bitrate: string | null
  status: string | null
  running: boolean
}

type ServerPreset = {
  id: string
  name: string
  description: string
  protocol: string
  default_port: number
  video_codec: string
  audio_codec: string
}

const DEFAULT_CLOUD: CloudSettings = {
  streaming: { enabled: false, server_url: '', protocol: 'webrtc', video_codec: 'h264', encryption: 'dtls-srtp' },
  recording: { enabled: false, use_byok: true, server_url: '', bucket: '', access_key: '', secret_key: '', region: '', storage_class: '' },
}

export function Cloud() {
  const [activeTab, setActiveTab] = useState<'streaming' | 's3'>('streaming')
  const [cfg, setCfg] = useState<CloudSettings>(DEFAULT_CLOUD)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // Cloud streaming state
  const [cameras, setCameras] = useState<Camera[]>([])
  const [streamTargets, setStreamTargets] = useState<StreamTarget[]>([])
  const [presets, setPresets] = useState<ServerPreset[]>([])
  const [showAddDialog, setShowAddDialog] = useState(false)
  const [editingTarget, setEditingTarget] = useState<{
    target_id?: string
    is_edit?: boolean
    camera_id: number
    server_url: string
    stream_key: string
    protocol: string
    use_tls: boolean
    use_custom_ca: boolean
    video_codec: string
    audio_codec: string
    video_bitrate: string
    audio_bitrate: string
  } | null>(null)
  const [showInfo, setShowInfo] = useState(false)

  useEffect(() => {
    loadData()
  }, [])

  async function loadData() {
    try {
      setLoading(true)
      setError(null)

      // Load cloud settings
      const { data: cloudData } = await apiService.getCloudSettings()
      setCfg({
        streaming: { ...DEFAULT_CLOUD.streaming, ...(cloudData.streaming || {}) },
        recording: { ...DEFAULT_CLOUD.recording, ...(cloudData.recording || {}) },
      })

      // Load cameras for streaming
      const { data: camerasData } = await apiService.getCameras()
      setCameras(camerasData.cameras || [])

      // Load stream targets
      const { data: targetsData } = await apiService.listStreamTargets()
      setStreamTargets(targetsData.targets || [])

      // Load server presets
      const { data: presetsData } = await apiService.getServerPresets()
      setPresets(presetsData.presets || [])

    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load cloud settings')
    } finally {
      setLoading(false)
    }
  }

  async function saveCloudSettings() {
    try {
      setLoading(true)
      setError(null)
      await apiService.updateCloudSettings(cfg)
      setNotice('Cloud settings saved')
      setTimeout(() => setNotice(null), 3000)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save cloud settings')
    } finally {
      setLoading(false)
    }
  }

  async function saveStreamTarget() {
    if (!editingTarget) return
    try {
      setLoading(true)
      setError(null)
      await apiService.createOrUpdateStreamTarget({
        target_id: editingTarget.target_id,
        camera_id: editingTarget.camera_id,
        enabled: false,
        server_url: editingTarget.server_url,
        stream_key: editingTarget.stream_key,
        protocol: editingTarget.protocol as any,
        use_tls: editingTarget.use_tls,
        use_custom_ca: editingTarget.use_custom_ca,
        video_codec: editingTarget.video_codec as any,
        audio_codec: editingTarget.audio_codec as any,
        video_bitrate: editingTarget.video_bitrate || null,
        audio_bitrate: editingTarget.audio_bitrate || '128k',
        max_reconnect_attempts: 5,
        reconnect_delay_seconds: 5,
      })
      setShowAddDialog(false)
      setEditingTarget(null)
      setNotice('Stream target saved')
      setTimeout(() => setNotice(null), 3000)
      await loadData()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save stream target')
    } finally {
      setLoading(false)
    }
  }

  async function deleteStreamTarget(targetId: string) {
    if (!confirm('Delete this stream target?')) return
    try {
      setLoading(true)
      await apiService.deleteStreamTarget(targetId)
      setNotice('Stream target deleted')
      setTimeout(() => setNotice(null), 3000)
      await loadData()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to delete')
    } finally {
      setLoading(false)
    }
  }

  async function startStream(targetId: string) {
    try {
      setLoading(true)
      await apiService.startStream(targetId)
      setNotice('Stream started')
      setTimeout(() => setNotice(null), 3000)
      await loadData()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to start stream')
    } finally {
      setLoading(false)
    }
  }

  async function stopStream(targetId: string) {
    try {
      setLoading(true)
      await apiService.stopStream(targetId)
      setNotice('Stream stopped')
      setTimeout(() => setNotice(null), 3000)
      await loadData()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to stop stream')
    } finally {
      setLoading(false)
    }
  }

  function openAddDialog() {
    const preset = presets.find(p => p.id === 'rtmp') || presets[0]
    setEditingTarget({
      is_edit: false,
      camera_id: cameras[0]?.id || 0,
      server_url: '',
      stream_key: '',
      protocol: preset?.protocol || 'rtmp',
      use_tls: false,
      use_custom_ca: false,
      video_codec: preset?.video_codec || 'copy',
      audio_codec: preset?.audio_codec || 'copy',
      video_bitrate: '',
      audio_bitrate: '128k',
    })
    setShowAddDialog(true)
  }

  function openEditDialog(target: StreamTarget) {
    setEditingTarget({
      target_id: target.target_id,
      is_edit: true,
      camera_id: target.camera_id,
      server_url: target.server_url,
      // We don't have the actual key; leave blank to preserve on backend
      stream_key: '',
      protocol: target.protocol,
      use_tls: target.protocol === 'rtmps',
      use_custom_ca: target.use_custom_ca,
      video_codec: target.video_codec,
      audio_codec: target.audio_codec,
      video_bitrate: target.video_bitrate || '',
      audio_bitrate: target.audio_bitrate || '128k',
    })
    setShowAddDialog(true)
  }

  function onProtocolChange(protocol: string) {
    if (!editingTarget) return
    const preset = presets.find(p => p.protocol === protocol)
    setEditingTarget({
      ...editingTarget,
      protocol,
      use_tls: protocol === 'rtmps',
      video_codec: preset?.video_codec || 'copy',
      audio_codec: preset?.audio_codec || 'copy',
    })
  }

  const getCameraName = (target: StreamTarget) => {
    return target.camera_name || `Camera ${target.camera_id}`
  }

  return (
    <section className="space-y-4">
      {/* Info Dialog */}
      {showInfo && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-lg w-full mx-4">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold flex items-center gap-2">
                <Info size={20} className="text-[var(--accent)]" />
                About Cloud Streaming
              </h3>
              <button onClick={() => setShowInfo(false)} className="text-[var(--text-dim)] hover:text-[var(--text)]">
                <X size={20} />
              </button>
            </div>
            <div className="space-y-4 text-sm">
              <div>
                <h4 className="font-medium text-[var(--text)] mb-1">What is Cloud Streaming?</h4>
                <p className="text-[var(--text-dim)]">
                  Push camera streams to your own streaming servers for real-time viewing 
                  on mobile apps, web dashboards, or other locations.
                </p>
              </div>
              <div>
                <h4 className="font-medium text-[var(--text)] mb-1">Supported Server Types</h4>
                <ul className="text-[var(--text-dim)] list-disc list-inside space-y-1">
                  <li><strong>RTMP:</strong> AntMedia, Nginx-RTMP, Wowza, Red5</li>
                  <li><strong>RTMPS:</strong> Secure RTMP with TLS encryption</li>
                  <li><strong>SRT:</strong> Low-latency streaming (Secure Reliable Transport)</li>
                </ul>
              </div>
              <div>
                <h4 className="font-medium text-[var(--text)] mb-1">Use Cases</h4>
                <ul className="text-[var(--text-dim)] list-disc list-inside space-y-1">
                  <li>Mobile app live viewing backend</li>
                  <li>Remote monitoring dashboards</li>
                  <li>Edge-to-cloud NVR replication</li>
                  <li>Multi-site stream distribution</li>
                </ul>
              </div>
              <div>
                <h4 className="font-medium text-[var(--text)] mb-1">TLS with Custom Certificates</h4>
                <p className="text-[var(--text-dim)]">
                  If your server uses a self-signed or private CA certificate, 
                  enable "Use Custom CA" and configure your certificate in the BYOK settings.
                </p>
              </div>
            </div>
            <div className="mt-6 flex justify-end">
              <button onClick={() => setShowInfo(false)} className="px-4 py-2 bg-[var(--accent)] text-white">
                Got it
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Add/Edit Stream Target Dialog */}
      {showAddDialog && editingTarget && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">{editingTarget.is_edit ? 'Edit Stream Target' : 'Configure Stream Target'}</h3>
            <div className="space-y-4">
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)] text-sm">Camera</span>
                <select 
                  className="select" 
                  value={editingTarget.camera_id}
                  onChange={(e) => setEditingTarget({...editingTarget, camera_id: parseInt(e.target.value)})}
                >
                  {cameras.map(cam => (
                    <option key={cam.id} value={cam.id}>{cam.name}</option>
                  ))}
                </select>
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)] text-sm">Protocol</span>
                <select 
                  className="select" 
                  value={editingTarget.protocol}
                  onChange={(e) => onProtocolChange(e.target.value)}
                >
                  <option value="rtmp">RTMP (Standard)</option>
                  <option value="rtmps">RTMPS (TLS Encrypted)</option>
                  <option value="srt">SRT (Low Latency)</option>
                </select>
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)] text-sm">Server URL</span>
                <input 
                  className="input" 
                  value={editingTarget.server_url}
                  onChange={(e) => setEditingTarget({...editingTarget, server_url: e.target.value})}
                  placeholder={editingTarget.protocol === 'srt' ? 'srt://server:9000' : 'rtmp://server/app'}
                />
                <span className="text-xs text-[var(--text-dim)]">
                  {editingTarget.protocol === 'rtmp' && 'e.g., rtmp://192.168.1.100/live'}
                  {editingTarget.protocol === 'rtmps' && 'e.g., rtmps://secure.server.com/live'}
                  {editingTarget.protocol === 'srt' && 'e.g., srt://192.168.1.100:9000'}
                </span>
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)] text-sm">Stream Key / Path (optional)</span>
                <input 
                  className="input" 
                  value={editingTarget.stream_key}
                  onChange={(e) => setEditingTarget({...editingTarget, stream_key: e.target.value})}
                  placeholder={editingTarget.is_edit ? 'Leave empty to keep existing key' : 'camera1 or leave empty'}
                />
              </label>

              {editingTarget.protocol === 'rtmps' && (
                <label className="flex items-center gap-2">
                  <input 
                    type="checkbox" 
                    className="accent-[var(--accent)]"
                    checked={editingTarget.use_custom_ca}
                    onChange={(e) => setEditingTarget({...editingTarget, use_custom_ca: e.target.checked})}
                  />
                  <span className="text-sm flex items-center gap-1">
                    <Shield size={14} />
                    Use BYOK CA certificate (for self-signed certs)
                  </span>
                </label>
              )}

              <div className="grid grid-cols-2 gap-2">
                <label className="flex flex-col gap-1">
                  <span className="text-[var(--text-dim)] text-sm">Video Codec</span>
                  <select 
                    className="select" 
                    value={editingTarget.video_codec}
                    onChange={(e) => setEditingTarget({...editingTarget, video_codec: e.target.value})}
                  >
                    <option value="copy">Copy (passthrough)</option>
                    <option value="libx264">H.264 (re-encode)</option>
                    <option value="libx265">H.265 (re-encode)</option>
                  </select>
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-[var(--text-dim)] text-sm">Video Bitrate</span>
                  <input 
                    className="input" 
                    value={editingTarget.video_bitrate}
                    onChange={(e) => setEditingTarget({...editingTarget, video_bitrate: e.target.value})}
                    placeholder="e.g., 4000k (optional)"
                    disabled={editingTarget.video_codec === 'copy'}
                  />
                </label>
              </div>
            </div>

            <div className="flex justify-end gap-2 mt-6">
              <button 
                className="px-4 py-2 border border-neutral-600 text-[var(--text-dim)]"
                onClick={() => { setShowAddDialog(false); setEditingTarget(null) }}
              >
                Cancel
              </button>
              <button 
                className="px-4 py-2 bg-[var(--accent)] text-white disabled:opacity-50"
                onClick={saveStreamTarget}
                disabled={loading || !editingTarget.server_url}
              >
                {loading ? <Loader2 className="animate-spin" size={16} /> : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold flex items-center gap-2">
          Cloud
          <button 
            onClick={() => setShowInfo(true)} 
            className="text-[var(--text-dim)] hover:text-[var(--accent)] transition-colors"
            title="Learn about Cloud Streaming"
          >
            <Info size={18} />
          </button>
        </h1>
      </div>

      {notice && (
        <div className="p-2 bg-green-500/10 border border-green-500/30 text-green-300 text-sm flex items-center gap-2">
          <CheckCircle size={16} /> {notice}
        </div>
      )}
      {error && (
        <div className="p-2 bg-red-500/10 border border-red-500/30 text-red-300 text-sm flex items-center gap-2">
          <AlertCircle size={16} /> {error}
        </div>
      )}

      {/* Tabs */}
      <div className="flex border-b border-neutral-700">
        <button
          className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px ${activeTab === 'streaming' ? 'border-[var(--accent)] text-[var(--accent)]' : 'border-transparent text-[var(--text-dim)]'}`}
          onClick={() => setActiveTab('streaming')}
        >
          Stream to Server
        </button>
        <button
          className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px ${activeTab === 's3' ? 'border-[var(--accent)] text-[var(--accent)]' : 'border-transparent text-[var(--text-dim)]'}`}
          onClick={() => setActiveTab('s3')}
        >
          Cloud Recording Server
        </button>
      </div>

      {/* Stream to Server Tab */}
      {activeTab === 'streaming' && (
        <div className="space-y-4">
          <div className="flex justify-between items-center">
            <p className="text-sm text-[var(--text-dim)]">
              Push camera streams to your custom RTMP/RTMPS/SRT servers
            </p>
            <button
              className="btn btn-primary flex items-center gap-2"
              onClick={openAddDialog}
              disabled={cameras.length === 0}
            >
              <Plus size={16} /> Add Stream
            </button>
          </div>

          {cameras.length === 0 ? (
            <div className="text-center py-8 text-[var(--text-dim)]">
              No cameras configured. Add cameras first to enable cloud streaming.
            </div>
          ) : streamTargets.length === 0 ? (
            <div className="text-center py-8 text-[var(--text-dim)]">
              No stream targets configured. Click "Add Stream" to get started.
            </div>
          ) : (
            <div className="space-y-2">
              {streamTargets.map(target => (
                <div key={target.target_id} className="bg-[var(--panel-2)] border border-neutral-700 p-4">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <Server size={20} className="text-[var(--accent)]" />
                      <div>
                        <div className="font-medium">{getCameraName(target)}</div>
                        <div className="text-sm text-[var(--text-dim)]">
                          {target.protocol.toUpperCase()} • {target.server_url}
                          {target.use_custom_ca && <span className="ml-2 text-xs text-amber-400">(BYOK CA)</span>}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      {/* Status indicator */}
                      <span className={`px-2 py-1 text-xs ${
                        target.running 
                          ? 'bg-green-500/20 text-green-400 border border-green-500/30' 
                          : target.status === 'error'
                          ? 'bg-red-500/20 text-red-400 border border-red-500/30'
                          : 'bg-neutral-500/20 text-neutral-400 border border-neutral-500/30'
                      }`}>
                        {target.running ? 'Streaming' : target.status || 'stopped'}
                      </span>

                      {/* Control buttons */}
                      {target.running ? (
                        <button
                          className="p-2 bg-red-500/20 text-red-400 hover:bg-red-500/30"
                          onClick={() => stopStream(target.target_id)}
                          title="Stop Stream"
                        >
                          <Square size={16} />
                        </button>
                      ) : (
                        <button
                          className="p-2 bg-green-500/20 text-green-400 hover:bg-green-500/30"
                          onClick={() => startStream(target.target_id)}
                          title="Start Stream"
                        >
                          <Play size={16} />
                        </button>
                      )}

                      <button
                        className="p-2 bg-[var(--panel)] border border-neutral-700 text-[var(--text-dim)] hover:text-[var(--text)] hover:border-neutral-600"
                        onClick={() => openEditDialog(target)}
                        title="Edit"
                        disabled={loading}
                      >
                        <Pencil size={16} />
                      </button>

                      <button
                        className="p-2 bg-red-500/10 text-red-400 hover:bg-red-500/20"
                        onClick={() => deleteStreamTarget(target.target_id)}
                        title="Delete"
                      >
                        <Trash2 size={16} />
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Cloud Recording Server Tab */}
      {activeTab === 's3' && (
        <div className="space-y-4">
          <div className="flex justify-end">
            <button className="btn btn-primary" onClick={saveCloudSettings} disabled={loading}>
              {loading ? <Loader2 className="animate-spin" size={16} /> : 'Save Settings'}
            </button>
          </div>

          <div className="bg-[var(--panel-2)] border border-neutral-700 p-4 space-y-4">
            <div className="font-medium">Cloud Recording Server</div>
            <p className="text-sm text-[var(--text-dim)]">
              Upload recordings to your cloud recording endpoint (AWS S3, MinIO, DigitalOcean Spaces, or other S3-compatible storage).
            </p>
            
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <label className="flex items-center gap-2 md:col-span-2">
                <input 
                  type="checkbox" 
                  className="accent-[var(--accent)]" 
                  checked={cfg.recording.enabled} 
                  onChange={(e) => setCfg(s => ({...s, recording: {...s.recording, enabled: e.target.checked}}))} 
                />
                <span>Enable Cloud Recording Upload</span>
              </label>

              <label className="flex items-center gap-2 md:col-span-2">
                <input
                  type="checkbox"
                  className="accent-[var(--accent)]"
                  checked={!!cfg.recording.use_byok}
                  onChange={(e) => setCfg(s => ({...s, recording: {...s.recording, use_byok: e.target.checked}}))}
                />
                <span>Use BYOK Certificates for Cloud Upload</span>
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)] text-sm">Endpoint URL</span>
                <input 
                  className="input" 
                  value={cfg.recording.server_url} 
                  onChange={(e) => setCfg(s => ({...s, recording: {...s.recording, server_url: e.target.value}}))} 
                  placeholder="https://s3.amazonaws.com or custom endpoint" 
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)] text-sm">Bucket Name</span>
                <input 
                  className="input" 
                  value={cfg.recording.bucket || ''} 
                  onChange={(e) => setCfg(s => ({...s, recording: {...s.recording, bucket: e.target.value}}))} 
                  placeholder="my-recordings-bucket" 
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)] text-sm">Access Key ID</span>
                <input 
                  className="input" 
                  value={cfg.recording.access_key || ''} 
                  onChange={(e) => setCfg(s => ({...s, recording: {...s.recording, access_key: e.target.value}}))} 
                  placeholder="AKIAIOSFODNN7EXAMPLE" 
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)] text-sm">Secret Access Key</span>
                <input 
                  className="input" 
                  type="password"
                  value={cfg.recording.secret_key || ''} 
                  onChange={(e) => setCfg(s => ({...s, recording: {...s.recording, secret_key: e.target.value}}))} 
                  placeholder="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" 
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)] text-sm">Region</span>
                <input 
                  className="input" 
                  value={cfg.recording.region || ''} 
                  onChange={(e) => setCfg(s => ({...s, recording: {...s.recording, region: e.target.value}}))} 
                  placeholder="us-east-1" 
                />
              </label>

              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)] text-sm">Storage Class</span>
                <select 
                  className="select" 
                  value={cfg.recording.storage_class || 'STANDARD'}
                  onChange={(e) => setCfg(s => ({...s, recording: {...s.recording, storage_class: e.target.value}}))}
                >
                  <option value="STANDARD">Standard</option>
                  <option value="STANDARD_IA">Standard-IA (Infrequent Access)</option>
                  <option value="ONEZONE_IA">One Zone-IA</option>
                  <option value="GLACIER">Glacier</option>
                  <option value="DEEP_ARCHIVE">Glacier Deep Archive</option>
                </select>
              </label>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
