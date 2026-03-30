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

import React, { useEffect, useMemo, useState } from 'react'
import { SuricataAlertStream } from './SuricataAlertStream'
import { useAuth } from '../auth/AuthContext'
import { apiService } from '../lib/apiService'

type CameraItem = { id: number; name: string; ip_address: string }

type AIConfig = {
  enabled: boolean
  provider: 'cpu' | 'gpu'
  max_concurrency: number
  object_detection: {
    enabled: boolean
    model: string
    confidence: number
    nms: number
    labels: string[]
    max_fps: number | null
    input_scale: { width: number | null; height: number | null }
  }
  motion_detection: {
    enabled: boolean
    sensitivity: number
    min_area: number
    debounce_ms: number
  }
  schedules: {
    enabled: boolean
    windows: Array<{ days: string[]; start: string; end: string }>
  }
  events: {
    create_alerts: boolean
    webhook_enabled: boolean
    webhook_url: string
  }
}

const DEFAULT_AI_CONFIG: AIConfig = {
  enabled: false,
  provider: 'cpu',
  max_concurrency: 2,
  object_detection: {
    enabled: false,
    model: 'yolo',
    confidence: 0.5,
    nms: 0.45,
    labels: ['person', 'car'],
    max_fps: null,
    input_scale: { width: null, height: null },
  },
  motion_detection: {
    enabled: true,
    sensitivity: 0.6,
    min_area: 600,
    debounce_ms: 2000,
  },
  schedules: {
    enabled: false,
    windows: [
      { days: ['Mon','Tue','Wed','Thu','Fri'], start: '08:00', end: '18:00' },
    ],
  },
  events: {
    create_alerts: true,
    webhook_enabled: false,
    webhook_url: '',
  },
}

type CameraOverride = {
  object_detection?: Partial<AIConfig['object_detection']>
  motion_detection?: Partial<AIConfig['motion_detection']>
}

const STORAGE_KEYS = {
  global: 'aiEngine.globalConfig.v1',
  overrides: 'aiEngine.cameraOverrides.v1',
}

function loadLocal<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    const parsed = JSON.parse(raw)
    return parsed as T
  } catch { return fallback }
}
function saveLocal<T>(key: string, value: T) {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch {}
}

function Section({ title, children, actions }: { title: string; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <div className="card">
      <div className="flex items-center gap-2 mb-3">
        <h2 className="text-base font-semibold">{title}</h2>
        <div className="ml-auto flex items-center gap-2">{actions}</div>
      </div>
      {children}
    </div>
  )
}

export function AIEngine() {
  const { user } = useAuth()
  const canAdmin = !!user?.is_superuser

  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const [cfg, setCfg] = useState<AIConfig>(() => loadLocal(STORAGE_KEYS.global, DEFAULT_AI_CONFIG))
  const [cameras, setCameras] = useState<CameraItem[]>([])
  const [overrides, setOverrides] = useState<Record<number, CameraOverride>>(() => loadLocal(STORAGE_KEYS.overrides, {} as Record<number, CameraOverride>))
  const [selectedCamId, setSelectedCamId] = useState<number | ''>('')

  const selectedOverride = useMemo<CameraOverride | null>(() => {
    if (selectedCamId === '') return null
    return overrides[selectedCamId] || {}
  }, [selectedCamId, overrides])

  useEffect(() => {
    // Load cameras for per-camera overrides
    let alive = true
    ;(async () => {
      try {
        const { data } = await apiService.getCameras({ limit: 200, active_only: true })
        const items = Array.isArray(data?.cameras) ? data.cameras : []
        if (alive) setCameras(items.map((c: any) => ({ id: c.id, name: c.name, ip_address: c.ip_address })))
      } catch {
        // ignore
      }
    })()
    return () => { alive = false }
  }, [])

  const saveAll = async () => {
    if (!canAdmin) return
    try {
      setLoading(true)
      setError(null)
      // Persist locally until backend endpoints exist
      saveLocal(STORAGE_KEYS.global, cfg)
      saveLocal(STORAGE_KEYS.overrides, overrides)
      // Optional: attempt backend persistence if route exists in future
      // await apiService.updateAISettings?.({ cfg, overrides })
      setNotice('AI Engine settings saved locally')
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save settings')
    } finally { setLoading(false) }
  }

  const resetAll = () => {
    if (!confirm('Reset AI Engine settings to defaults?')) return
    setCfg(DEFAULT_AI_CONFIG)
    setOverrides({})
    saveLocal(STORAGE_KEYS.global, DEFAULT_AI_CONFIG)
    saveLocal(STORAGE_KEYS.overrides, {})
    setNotice('AI Engine settings reset to defaults')
  }

  const updateOverride = (camId: number, patch: CameraOverride) => {
    setOverrides((prev) => {
      const next = { ...prev, [camId]: { ...prev[camId], ...patch } }
      return next
    })
  }

  return (
    <div className="space-y-4">
      <SuricataAlertStream />
  <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Anomaly AI Engine</h1>
          <p className="text-[var(--text-dim)]">Configure AI-powered motion/object detection, schedules, and alerting.</p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn" onClick={resetAll} disabled={!canAdmin || loading}>Reset</button>
          <button className="btn btn-primary disabled:opacity-50" onClick={saveAll} disabled={!canAdmin || loading}>{loading ? 'Saving…' : 'Save'}</button>
        </div>
      </div>

      {!canAdmin && (
        <div className="p-2 rounded bg-amber-500/10 border border-amber-500/30 text-amber-300 text-sm">
          You have read-only access. Only administrators can modify AI settings.
        </div>
      )}

      <div className="p-2 rounded bg-blue-500/10 border border-blue-500/30 text-blue-300 text-xs">
        Backend endpoints for AI are not wired yet. Settings are stored in your browser until the server API is available.
      </div>

      {notice && (
        <div className="p-2 rounded bg-green-500/10 border border-green-500/30 text-green-300 text-sm">{notice}</div>
      )}
      {error && (
        <div className="p-2 rounded bg-red-500/10 border border-red-500/30 text-red-300 text-sm">{error}</div>
      )}

      {/* Global controls */}
      <Section title="Engine" actions={
        <>
          <label className="inline-flex items-center gap-2 text-sm">
            <input type="checkbox" className="accent-[var(--accent)]" checked={cfg.enabled} onChange={(e)=>setCfg({ ...cfg, enabled: e.target.checked })} disabled={!canAdmin} /> Enabled
          </label>
        </>
      }>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
          <label className="flex items-center justify-between gap-2">
            <span>Provider</span>
            <select className="w-44 select" value={cfg.provider} onChange={(e)=>setCfg({ ...cfg, provider: e.target.value as 'cpu'|'gpu' })} disabled={!canAdmin}>
              <option value="cpu">CPU</option>
              <option value="gpu">GPU</option>
            </select>
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Max concurrency</span>
            <input type="number" className="w-24 input" min={1} max={16} value={cfg.max_concurrency} onChange={(e)=>setCfg({ ...cfg, max_concurrency: Math.max(1, Number(e.target.value)||1) })} disabled={!canAdmin} />
          </label>
          <div className="text-[var(--text-dim)] text-xs">Controls how many video streams are analyzed in parallel.</div>
        </div>
      </Section>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {/* Motion detection */}
        <Section title="Motion detection">
          <div className="space-y-2 text-sm">
            <label className="inline-flex items-center gap-2">
              <input type="checkbox" className="accent-[var(--accent)]" checked={cfg.motion_detection.enabled} onChange={(e)=>setCfg({ ...cfg, motion_detection: { ...cfg.motion_detection, enabled: e.target.checked } })} disabled={!canAdmin} /> Enable motion detection
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="flex items-center justify-between gap-2">
                <span>Sensitivity</span>
                <input type="range" min={0} max={1} step={0.05} className="w-40" value={cfg.motion_detection.sensitivity} onChange={(e)=>setCfg({ ...cfg, motion_detection: { ...cfg.motion_detection, sensitivity: Number(e.target.value) } })} disabled={!canAdmin} />
              </label>
              <label className="flex items-center justify-between gap-2">
                <span>Min area (px)</span>
                <input type="number" className="w-32 input" value={cfg.motion_detection.min_area} onChange={(e)=>setCfg({ ...cfg, motion_detection: { ...cfg.motion_detection, min_area: Math.max(0, Number(e.target.value)||0) } })} disabled={!canAdmin} />
              </label>
              <label className="flex items-center justify-between gap-2">
                <span>Debounce (ms)</span>
                <input type="number" className="w-32 input" value={cfg.motion_detection.debounce_ms} onChange={(e)=>setCfg({ ...cfg, motion_detection: { ...cfg.motion_detection, debounce_ms: Math.max(0, Number(e.target.value)||0) } })} disabled={!canAdmin} />
              </label>
            </div>
            <div className="text-[var(--text-dim)] text-xs">Tune to reduce noise while keeping true motion events.</div>
          </div>
        </Section>

        {/* Object detection */}
        <Section title="Object detection">
          <div className="space-y-2 text-sm">
            <label className="inline-flex items-center gap-2">
              <input type="checkbox" className="accent-[var(--accent)]" checked={cfg.object_detection.enabled} onChange={(e)=>setCfg({ ...cfg, object_detection: { ...cfg.object_detection, enabled: e.target.checked } })} disabled={!canAdmin} /> Enable object detection
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="flex items-center justify-between gap-2">
                <span>Model</span>
                <select className="w-40 select" value={cfg.object_detection.model} onChange={(e)=>setCfg({ ...cfg, object_detection: { ...cfg.object_detection, model: e.target.value } })} disabled={!canAdmin}>
                  <option value="yolo">YOLO</option>
                  <option value="mobilenet">MobileNet</option>
                  <option value="custom">Custom</option>
                </select>
              </label>
              <label className="flex items-center justify-between gap-2">
                <span>Confidence</span>
                <input type="range" min={0} max={1} step={0.01} className="w-40" value={cfg.object_detection.confidence} onChange={(e)=>setCfg({ ...cfg, object_detection: { ...cfg.object_detection, confidence: Number(e.target.value) } })} disabled={!canAdmin} />
              </label>
              <label className="flex items-center justify-between gap-2">
                <span>NMS</span>
                <input type="range" min={0} max={1} step={0.01} className="w-40" value={cfg.object_detection.nms} onChange={(e)=>setCfg({ ...cfg, object_detection: { ...cfg.object_detection, nms: Number(e.target.value) } })} disabled={!canAdmin} />
              </label>
              <label className="flex items-center justify-between gap-2">
                <span>Max FPS</span>
                <input type="number" className="w-28 input" value={cfg.object_detection.max_fps ?? ''} onChange={(e)=>setCfg({ ...cfg, object_detection: { ...cfg.object_detection, max_fps: e.target.value ? Number(e.target.value) : null } })} disabled={!canAdmin} />
              </label>
              <label className="flex items-center justify-between gap-2">
                <span>Scale (w×h)</span>
                <div className="flex items-center gap-2">
                  <input type="number" className="w-24 input" value={cfg.object_detection.input_scale.width ?? ''} onChange={(e)=>setCfg({ ...cfg, object_detection: { ...cfg.object_detection, input_scale: { ...cfg.object_detection.input_scale, width: e.target.value ? Number(e.target.value) : null } } })} disabled={!canAdmin} />
                  <span>×</span>
                  <input type="number" className="w-24 input" value={cfg.object_detection.input_scale.height ?? ''} onChange={(e)=>setCfg({ ...cfg, object_detection: { ...cfg.object_detection, input_scale: { ...cfg.object_detection.input_scale, height: e.target.value ? Number(e.target.value) : null } } })} disabled={!canAdmin} />
                </div>
              </label>
              <label className="flex items-center justify-between gap-2 col-span-2">
                <span>Detect classes</span>
                <input className="flex-1 input" value={cfg.object_detection.labels.join(', ')} onChange={(e)=>setCfg({ ...cfg, object_detection: { ...cfg.object_detection, labels: e.target.value.split(',').map(s=>s.trim()).filter(Boolean) } })} disabled={!canAdmin} placeholder="comma-separated e.g. person, car, dog" />
              </label>
            </div>
            <div className="text-[var(--text-dim)] text-xs">Choose classes of interest and tune accuracy/speed trade-offs.</div>
          </div>
        </Section>
      </div>

      {/* Schedules and events */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <Section title="Schedules">
          <div className="space-y-2 text-sm">
            <label className="inline-flex items-center gap-2">
              <input type="checkbox" className="accent-[var(--accent)]" checked={cfg.schedules.enabled} onChange={(e)=>setCfg({ ...cfg, schedules: { ...cfg.schedules, enabled: e.target.checked } })} disabled={!canAdmin} /> Enable schedules
            </label>
            {cfg.schedules.windows.map((w, idx) => (
              <div key={idx} className="grid grid-cols-3 gap-2 items-center">
                <input className="input" value={w.days.join(',')} onChange={(e)=>{
                  const v = e.target.value.split(',').map(s=>s.trim()).filter(Boolean)
                  const windows = [...cfg.schedules.windows]; windows[idx] = { ...w, days: v }; setCfg({ ...cfg, schedules: { ...cfg.schedules, windows } })
                }} disabled={!canAdmin} />
                <input type="time" className="input" value={w.start} onChange={(e)=>{ const windows = [...cfg.schedules.windows]; windows[idx] = { ...w, start: e.target.value }; setCfg({ ...cfg, schedules: { ...cfg.schedules, windows } }) }} disabled={!canAdmin} />
                <div className="flex items-center gap-2">
                  <input type="time" className="input" value={w.end} onChange={(e)=>{ const windows = [...cfg.schedules.windows]; windows[idx] = { ...w, end: e.target.value }; setCfg({ ...cfg, schedules: { ...cfg.schedules, windows } }) }} disabled={!canAdmin} />
                  <button className="btn" onClick={()=>{ const windows = cfg.schedules.windows.filter((_,i)=>i!==idx); setCfg({ ...cfg, schedules: { ...cfg.schedules, windows } }) }} disabled={!canAdmin}>Remove</button>
                </div>
              </div>
            ))}
            <button className="btn" onClick={()=> setCfg({ ...cfg, schedules: { ...cfg.schedules, windows: [...cfg.schedules.windows, { days: ['Sat','Sun'], start: '00:00', end: '23:59' }] } })} disabled={!canAdmin}>Add window</button>
            <div className="text-[var(--text-dim)] text-xs">Days: Mon,Tue,Wed,Thu,Fri,Sat,Sun</div>
          </div>
        </Section>

        <Section title="Events & Webhooks">
          <div className="space-y-2 text-sm">
            <label className="inline-flex items-center gap-2">
              <input type="checkbox" className="accent-[var(--accent)]" checked={cfg.events.create_alerts} onChange={(e)=>setCfg({ ...cfg, events: { ...cfg.events, create_alerts: e.target.checked } })} disabled={!canAdmin} /> Create Alerts/Incidents for detections
            </label>
            <label className="inline-flex items-center gap-2">
              <input type="checkbox" className="accent-[var(--accent)]" checked={cfg.events.webhook_enabled} onChange={(e)=>setCfg({ ...cfg, events: { ...cfg.events, webhook_enabled: e.target.checked } })} disabled={!canAdmin} /> Send webhook
            </label>
            <label className="flex items-center justify-between gap-2">
              <span>Webhook URL</span>
              <input className="w-[28rem] input" value={cfg.events.webhook_url} onChange={(e)=>setCfg({ ...cfg, events: { ...cfg.events, webhook_url: e.target.value } })} disabled={!canAdmin || !cfg.events.webhook_enabled} />
            </label>
            <div className="text-[var(--text-dim)] text-xs">Webhook will receive JSON payloads for motion/object detections.</div>
          </div>
        </Section>
      </div>

      {/* Per-camera overrides */}
      <Section title="Per-camera overrides" actions={
        <label className="inline-flex items-center gap-2 text-sm">
          <span>Camera</span>
          <select className="w-56 select" value={selectedCamId} onChange={(e)=> setSelectedCamId(e.target.value ? Number(e.target.value) : '')}>
            <option value="">(select)</option>
            {cameras.map(c => <option key={c.id} value={c.id}>{c.name} · {c.ip_address}</option>)}
          </select>
        </label>
      }>
        {selectedCamId === '' ? (
          <div className="text-sm text-[var(--text-dim)]">Pick a camera to customize settings.</div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
            <div className="space-y-2">
              <div className="font-medium">Motion detection</div>
              <label className="inline-flex items-center gap-2">
                <input type="checkbox" className="accent-[var(--accent)]" checked={!!selectedOverride?.motion_detection?.enabled} onChange={(e)=> updateOverride(selectedCamId as number, { motion_detection: { ...selectedOverride?.motion_detection, enabled: e.target.checked } })} disabled={!canAdmin} /> Override enable
              </label>
              <label className="flex items-center justify-between gap-2">
                <span>Sensitivity</span>
                <input type="range" min={0} max={1} step={0.05} className="w-40" value={selectedOverride?.motion_detection?.sensitivity ?? cfg.motion_detection.sensitivity} onChange={(e)=> updateOverride(selectedCamId as number, { motion_detection: { ...selectedOverride?.motion_detection, sensitivity: Number(e.target.value) } })} disabled={!canAdmin} />
              </label>
              <label className="flex items-center justify-between gap-2">
                <span>Min area</span>
                <input type="number" className="w-28 input" value={selectedOverride?.motion_detection?.min_area ?? cfg.motion_detection.min_area} onChange={(e)=> updateOverride(selectedCamId as number, { motion_detection: { ...selectedOverride?.motion_detection, min_area: Math.max(0, Number(e.target.value)||0) } })} disabled={!canAdmin} />
              </label>
            </div>
            <div className="space-y-2">
              <div className="font-medium">Object detection</div>
              <label className="inline-flex items-center gap-2">
                <input type="checkbox" className="accent-[var(--accent)]" checked={!!selectedOverride?.object_detection?.enabled} onChange={(e)=> updateOverride(selectedCamId as number, { object_detection: { ...selectedOverride?.object_detection, enabled: e.target.checked } })} disabled={!canAdmin} /> Override enable
              </label>
              <label className="flex items-center justify-between gap-2">
                <span>Confidence</span>
                <input type="range" min={0} max={1} step={0.01} className="w-40" value={selectedOverride?.object_detection?.confidence ?? cfg.object_detection.confidence} onChange={(e)=> updateOverride(selectedCamId as number, { object_detection: { ...selectedOverride?.object_detection, confidence: Number(e.target.value) } })} disabled={!canAdmin} />
              </label>
              <label className="flex items-center justify-between gap-2">
                <span>Classes</span>
                <input className="w-64 input" value={(selectedOverride?.object_detection?.labels ?? cfg.object_detection.labels).join(', ')} onChange={(e)=> updateOverride(selectedCamId as number, { object_detection: { ...selectedOverride?.object_detection, labels: e.target.value.split(',').map(s=>s.trim()).filter(Boolean) } })} disabled={!canAdmin} />
              </label>
              <div className="flex items-center gap-2">
                <button className="btn" onClick={()=> {
                  const next = { ...overrides }; delete next[selectedCamId as number]; setOverrides(next); saveLocal(STORAGE_KEYS.overrides, next)
                }} disabled={!canAdmin}>Clear override</button>
                <button className="btn" onClick={()=> { saveLocal(STORAGE_KEYS.overrides, overrides); setNotice(`Saved override for camera ${selectedCamId}`) }} disabled={!canAdmin}>Save override</button>
              </div>
            </div>
          </div>
        )}
      </Section>

      {/* Advanced */}
      <Section title="Advanced">
        <div className="text-sm space-y-2">
          <div className="text-[var(--text-dim)]">JSON export/import</div>
          <div className="flex items-center gap-2">
            <button className="btn" onClick={() => {
              const blob = new Blob([JSON.stringify({ cfg, overrides }, null, 2)], { type: 'application/json' })
              const url = URL.createObjectURL(blob)
              const a = document.createElement('a'); a.href = url; a.download = 'ai-config.json'; a.click(); URL.revokeObjectURL(url)
            }}>Export</button>
            <label className="btn inline-flex items-center gap-2 cursor-pointer">
              <input type="file" accept="application/json" className="hidden" onChange={(e) => {
                const file = e.target.files?.[0]; if (!file) return
                const reader = new FileReader(); reader.onload = () => {
                  try {
                    const parsed = JSON.parse(String(reader.result || '{}'))
                    if (parsed.cfg) setCfg(parsed.cfg)
                    if (parsed.overrides) setOverrides(parsed.overrides)
                    setNotice('Imported AI config from file')
                  } catch { setError('Invalid JSON file') }
                }; reader.readAsText(file)
              }} />
              Import
            </label>
            <button className="btn" onClick={()=> alert('Test pipeline not implemented yet')}>Test pipeline</button>
          </div>
        </div>
      </Section>
    </div>
  )
}
