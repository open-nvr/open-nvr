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
import { apiService } from '../lib/apiService'
import { useAuth } from '../auth/AuthContext'
import { ChevronDown, Check, X, RotateCcw, AlertTriangle, FileText, Settings } from 'lucide-react'

type Jsonish = any
type ConfigSection = 'global' | 'defaults' | 'streams'

function TextAreaJson({ value, onChange, disabled }: { value: string; onChange: (v: string) => void; disabled?: boolean }) {
  return (
    <textarea
      className="w-full h-96 font-mono text-xs p-3 rounded border border-[var(--border)] bg-[var(--bg)] focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none transition-colors"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled}
      spellCheck={false}
    />
  )
}

function Switch({ label, checked, onChange, disabled }: { label: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <div className="flex items-center justify-between py-2">
      <span className="text-sm font-medium">{label}</span>
      <button
        type="button"
        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 ${checked ? 'bg-blue-600' : 'bg-gray-700'} ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
        onClick={() => !disabled && onChange(!checked)}
        disabled={disabled}
      >
        <span
          className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${checked ? 'translate-x-6' : 'translate-x-1'}`}
        />
      </button>
    </div>
  )
}

function Input({ label, value, onChange, disabled, placeholder = '' }: { label: string; value: string; onChange: (v: string) => void; disabled?: boolean; placeholder?: string }) {
  return (
    <div className="py-2">
      <label className="block text-sm font-medium mb-1">{label}</label>
      <input
        type="text"
        className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:opacity-50"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        placeholder={placeholder}
      />
    </div>
  )
}

function Select({ label, value, onChange, options, disabled }: { label: string; value: string; onChange: (v: string) => void; options: { value: string; label: string }[]; disabled?: boolean }) {
  return (
    <div className="py-2">
      <label className="block text-sm font-medium mb-1">{label}</label>
      <div className="relative">
        <select
          className="w-full appearance-none rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:opacity-50"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
        >
          {options.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        <div className="pointer-events-none absolute inset-y-0 right-0 flex items-center px-2 text-[var(--text-dim)]">
          <ChevronDown size={14} />
        </div>
      </div>
    </div>
  )
}

function PathDefaultsForm({ config, onChange, disabled }: { config: any; onChange: (newConfig: any) => void; disabled?: boolean }) {
  const handleChange = (key: string, value: any) => {
    onChange({ ...config, [key]: value })
  }

  const transportOptions = [
    { value: 'tcp', label: 'TCP (More Reliable)' },
    { value: 'udp', label: 'UDP (Faster)' },
    { value: 'udpMulticast', label: 'UDP Multicast' },
    { value: 'http', label: 'HTTP Tunneling' },
  ]

  const formatOptions = [
    { value: 'fmp4', label: 'fMP4 (Fragmented MP4)' },
    { value: 'mpegts', label: 'MPEG-TS' },
    { value: 'mkv', label: 'Matroska (MKV)' },
  ]

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2">
      <div className="col-span-1 md:col-span-2 pb-2 mb-2 border-b border-[var(--border)] text-xs text-[var(--text-dim)] uppercase tracking-wider font-semibold">
        Recording Settings
      </div>

      <Switch 
        label="Enable Recording" 
        checked={config.record ?? false} 
        onChange={(v) => handleChange('record', v)} 
        disabled={disabled} 
      />
      <Select 
        label="Container Format" 
        value={config.recordFormat ?? ''} 
        onChange={(v) => handleChange('recordFormat', v)} 
        options={formatOptions} 
        disabled={disabled} 
      />
      <Input 
        label="Storage Path Pattern" 
        value={config.recordPath ?? ''} 
        onChange={(v) => handleChange('recordPath', v)} 
        disabled={disabled} 
        placeholder="/data/recordings/%path/%Y/%m/%d/..."
      />
      <Input 
        label="Segment Duration" 
        value={config.recordSegmentDuration ?? ''} 
        onChange={(v) => handleChange('recordSegmentDuration', v)} 
        disabled={disabled} 
        placeholder="1h0m0s"
      />
      <Input 
        label="Part Duration" 
        value={config.recordPartDuration ?? ''} 
        onChange={(v) => handleChange('recordPartDuration', v)} 
        disabled={disabled} 
        placeholder="1s"
      />
      <Input 
        label="Retention (Delete After)" 
        value={config.recordDeleteAfter ?? ''} 
        onChange={(v) => handleChange('recordDeleteAfter', v)} 
        disabled={disabled} 
        placeholder="7d (Leave empty to disable auto-delete)"
      />

      <div className="col-span-1 md:col-span-2 pt-4 pb-2 mb-2 border-b border-[var(--border)] text-xs text-[var(--text-dim)] uppercase tracking-wider font-semibold">
        Stream Settings
      </div>

      <Select 
        label="RTSP Transport Protocol" 
        value={config.rtspTransport ?? 'tcp'} 
        onChange={(v) => handleChange('rtspTransport', v)} 
        options={transportOptions} 
        disabled={disabled} 
      />
      <Switch 
        label="Source On Demand" 
        checked={config.sourceOnDemand ?? false} 
        onChange={(v) => handleChange('sourceOnDemand', v)} 
        disabled={disabled} 
      />
       <Input 
        label="Source On Demand Start Timeout" 
        value={config.sourceOnDemandStartTimeout ?? '10s'} 
        onChange={(v) => handleChange('sourceOnDemandStartTimeout', v)} 
        disabled={disabled} 
      />
       <Input 
        label="Source On Demand Close After" 
        value={config.sourceOnDemandCloseAfter ?? '10s'} 
        onChange={(v) => handleChange('sourceOnDemandCloseAfter', v)} 
        disabled={disabled} 
      />
      <Input 
        label="Max Viewers (0 = unlimited)" 
        value={String(config.maxReaders ?? 0)} 
        onChange={(v) => handleChange('maxReaders', Number(v) || 0)} 
        disabled={disabled} 
      />
    </div>
  )
}

function GlobalForm({ config, onChange, disabled }: { config: any; onChange: (newConfig: any) => void; disabled?: boolean }) {
  const handleChange = (key: string, value: any) => {
    onChange({ ...config, [key]: value })
  }

  const logLevelOptions = [
    { value: 'error', label: 'Error (Least Verbose)' },
    { value: 'warn', label: 'Warning' },
    { value: 'info', label: 'Info (Standard)' },
    { value: 'debug', label: 'Debug (Most Verbose)' },
  ]

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2">
       <div className="col-span-1 md:col-span-2 pb-2 mb-2 border-b border-[var(--border)] text-xs text-[var(--text-dim)] uppercase tracking-wider font-semibold">
        Protocols
      </div>
      <Switch label="Enable RTSP" checked={config.rtsp ?? false} onChange={(v) => handleChange('rtsp', v)} disabled={disabled} />
      <Switch label="Enable HLS" checked={config.hls ?? false} onChange={(v) => handleChange('hls', v)} disabled={disabled} />
      <Switch label="Enable WebRTC" checked={config.webrtc ?? false} onChange={(v) => handleChange('webrtc', v)} disabled={disabled} />
      <Switch label="Enable RTMP" checked={config.rtmp ?? false} onChange={(v) => handleChange('rtmp', v)} disabled={disabled} />

      <div className="col-span-1 md:col-span-2 pt-4 pb-2 mb-2 border-b border-[var(--border)] text-xs text-[var(--text-dim)] uppercase tracking-wider font-semibold">
        System & Logging
      </div>

      <Select label="Log Level" value={config.logLevel ?? 'info'} onChange={(v) => handleChange('logLevel', v)} options={logLevelOptions} disabled={disabled} />
      <Input label="Read Timeout" value={config.readTimeout ?? ''} onChange={(v) => handleChange('readTimeout', v)} disabled={disabled} placeholder="10s" />
      <Input label="Write Timeout" value={config.writeTimeout ?? ''} onChange={(v) => handleChange('writeTimeout', v)} disabled={disabled} placeholder="10s" />
    </div>
  )
}

export function Updates() {
  const { user } = useAuth()
  const canAdmin = !!user?.is_superuser
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  
  const [activeSection, setActiveSection] = useState<ConfigSection>('defaults')
  const [viewMode, setViewMode] = useState<'form' | 'json'>('form')

  const [health, setHealth] = useState<{ status?: string; version?: string } | null>(null)

  const [globalCfg, setGlobalCfg] = useState<Jsonish | null>(null)
  const [globalDraft, setGlobalDraft] = useState('')
  const [globalSaving, setGlobalSaving] = useState(false)

  const [pdCfg, setPdCfg] = useState<Jsonish | null>(null)
  const [pdDraft, setPdDraft] = useState('')
  const [pdSaving, setPdSaving] = useState(false)

  const [paths, setPaths] = useState<any[]>([])
  const [pathsLoading, setPathsLoading] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    let mounted = true
    ;(async () => {
      try {
        setLoading(true)
        setError(null)
        const [h, g, pd, pl] = await Promise.allSettled([
          apiService.getHealth(),
          apiService.mtxGlobalGet(),
          apiService.mtxPathdefaultsGet(),
          apiService.mtxPathsList(),
        ])
        if (!mounted) return
        if (h.status === 'fulfilled') setHealth(h.value.data)
        if (g.status === 'fulfilled') {
          const details = g.value.data?.details ?? g.value.data
          setGlobalCfg(details)
          try { setGlobalDraft(JSON.stringify(details, null, 2)) } catch { setGlobalDraft('') }
        }
        if (pd.status === 'fulfilled') {
          const details = pd.value.data?.details ?? pd.value.data
          setPdCfg(details)
          try { setPdDraft(JSON.stringify(details, null, 2)) } catch { setPdDraft('') }
        }
        if (pl.status === 'fulfilled') {
          const details = pl.value.data?.details ?? pl.value.data
          const items = Array.isArray(details?.items) ? details.items : []
          setPaths(items)
        }
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load configuration data')
      } finally {
        setLoading(false)
      }
    })()
    return () => {
      mounted = false
    }
  }, [])

  const globalValid = useMemo(() => {
    try { JSON.parse(globalDraft || '{}'); return true } catch { return false }
  }, [globalDraft])
  const pdValid = useMemo(() => {
    try { JSON.parse(pdDraft || '{}'); return true } catch { return false }
  }, [pdDraft])

  // Sync draft back to object for Form view
  const currentGlobalObj = useMemo(() => {
     try { return JSON.parse(globalDraft || '{}') } catch { return globalCfg || {} }
  }, [globalDraft, globalCfg])

  const currentPdObj = useMemo(() => {
     try { return JSON.parse(pdDraft || '{}') } catch { return pdCfg || {} }
  }, [pdDraft, pdCfg])


  async function saveGlobal() {
    try {
      setGlobalSaving(true)
      const payload = JSON.parse(globalDraft || '{}')
      const res = await apiService.mtxGlobalPatch(payload)
      const details = res.data?.details ?? res.data
      setGlobalCfg(details)
      setNotice('Global configuration updated successfully')
      setTimeout(() => setNotice(null), 3000)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to update global configuration')
    } finally { setGlobalSaving(false) }
  }

  async function savePathDefaults() {
    try {
      setPdSaving(true)
      const payload = JSON.parse(pdDraft || '{}')
      const res = await apiService.mtxPathdefaultsPatch(payload)
      const details = res.data?.details ?? res.data
      setPdCfg(details)
      setNotice('Path defaults updated successfully')
      setTimeout(() => setNotice(null), 3000)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to update path defaults')
    } finally { setPdSaving(false) }
  }

  // Handle Form Updates
  const handleGlobalFormChange = (newCfg: any) => {
    setGlobalDraft(JSON.stringify(newCfg, null, 2))
  }
  const handlePdFormChange = (newCfg: any) => {
    setPdDraft(JSON.stringify(newCfg, null, 2))
  }

  async function refreshPaths() {
    try {
      setPathsLoading(true)
      const res = await apiService.mtxPathsList()
      const details = res.data?.details ?? res.data
      const items = Array.isArray(details?.items) ? details.items : []
      setPaths(items)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load active paths')
    } finally { setPathsLoading(false) }
  }

  async function toggleRecording(cameraId: number, enabled: boolean) {
    try {
      if (enabled) await apiService.mtxEnableRecording(cameraId)
      else await apiService.mtxDisableRecording(cameraId)
      await refreshPaths()
      setNotice(`Recording ${enabled ? 'enabled' : 'disabled'} for camera ${cameraId}`)
      setTimeout(() => setNotice(null), 3000)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to toggle recording')
    }
  }

  return (
    <div className="space-y-6 max-w-5xl mx-auto pb-10">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Media Server Configuration</h1>
          <p className="text-[var(--text-dim)] mt-1">Manage core settings, protocols, and stream behaviors.</p>
        </div>
        <div className="text-xs text-[var(--text-dim)] bg-white/5 px-3 py-1.5 rounded-full border border-white/10 flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${health ? 'bg-green-500' : 'bg-amber-500'}`}></span>
            {health?.version ? <>Server v{health.version}</> : 'Connecting...'}
        </div>
      </div>

      {!canAdmin && (
        <div className="p-3 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-300 text-sm flex items-center gap-2">
           <AlertTriangle size={16} />
          Restricted Access: Read-only mode enabled.
        </div>
      )}

      {notice && (
        <div className="p-3 rounded-lg bg-green-500/10 border border-green-500/20 text-green-300 text-sm flex items-center gap-2 animate-pulse">
           <Check size={16} />
          {notice}
        </div>
      )}
      {error && (
        <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-300 text-sm flex items-center gap-2">
           <X size={16} />
          {error}
        </div>
      )}

      {/* Control Bar */}
      <div className="flex items-center gap-4 bg-[var(--card-bg)] p-4 rounded-xl border border-[var(--border)] shadow-sm flex-wrap">
        <div className="flex-1 min-w-[200px]">
        <label className="text-xs font-semibold text-[var(--text-dim)] uppercase tracking-wider mb-1 block">Configuration Section</label>
        <div className="relative">
          <select 
            className="w-full appearance-none bg-[var(--bg)] border border-[var(--border)] rounded-lg py-2 pl-3 pr-10 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500/50"
            value={activeSection}
            onChange={(e) => setActiveSection(e.target.value as ConfigSection)}
          >
            <option value="defaults">Path Defaults (Recording & Streams)</option>
            <option value="global">Global Settings (Protocols & Logging)</option>
            <option value="streams">Active Streams Monitor</option>
          </select>
          <div className="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none text-[var(--text-dim)]">
             <ChevronDown size={14} />
          </div>
        </div>
        </div>

        {activeSection !== 'streams' && (
             <div className="min-w-[150px]">
                <label className="text-xs font-semibold text-[var(--text-dim)] uppercase tracking-wider mb-1 block">Edit Mode</label>
                 <div className="flex bg-[var(--bg)] rounded-lg p-1 border border-[var(--border)]">
                    <button 
                        className={`flex-1 flex items-center justify-center py-1 px-3 text-xs rounded-md transition-colors ${viewMode === 'form' ? 'bg-[var(--panel-2)] text-white shadow-sm' : 'text-[var(--text-dim)] hover:text-[var(--text)]'}`}
                        onClick={() => setViewMode('form')}
                    >
                        <Settings size={12} className="mr-1" /> Easy
                    </button>
                    <button 
                        className={`flex-1 flex items-center justify-center py-1 px-3 text-xs rounded-md transition-colors ${viewMode === 'json' ? 'bg-[var(--panel-2)] text-white shadow-sm' : 'text-[var(--text-dim)] hover:text-[var(--text)]'}`}
                        onClick={() => setViewMode('json')}
                    >
                         <FileText size={12} className="mr-1" /> JSON
                    </button>
                 </div>
             </div>
        )}
      </div>

      <div className="min-h-[400px]">
        {/* Global config */}
        {activeSection === 'global' && (
          <div className="card p-6 animate-in fade-in slide-in-from-bottom-2 duration-300">
            <div className="flex items-center justify-between mb-6 pb-4 border-b border-[var(--border)]">
              <div>
                <h2 className="text-lg font-medium">Global Settings</h2>
                <p className="text-xs text-[var(--text-dim)]">Configure protocols, logging, and system timeouts.</p>
              </div>
              <div className="flex items-center gap-3">
                <button
                  className="btn bg-[var(--bg)] hover:bg-[var(--bg-hover)] border border-[var(--border)]"
                  onClick={() => setGlobalDraft(JSON.stringify(globalCfg ?? {}, null, 2))}
                  disabled={loading}
                ><RotateCcw size={14} className="mr-2" /> Revert</button>
                <button
                  className="btn btn-primary min-w-[100px]"
                  disabled={!canAdmin || !globalValid || globalSaving}
                  onClick={saveGlobal}
                >{globalSaving ? 'Saving…' : 'Save Changes'}</button>
              </div>
            </div>
            
            {viewMode === 'form' ? (
                 <GlobalForm config={currentGlobalObj} onChange={handleGlobalFormChange} disabled={loading || !canAdmin} />
            ) : (
                <>
                    <TextAreaJson value={globalDraft} onChange={setGlobalDraft} disabled={loading || !canAdmin} />
                    {!globalValid && <div className="mt-2 text-xs text-red-400 font-medium">⚠ Invalid JSON: Please fix syntax errors before saving.</div>}
                </>
            )}
            
            <div className="mt-6 text-xs text-[var(--text-dim)] bg-[var(--bg)] p-3 rounded border border-[var(--border)]">
              <span className="font-semibold text-amber-500 mr-1">Note:</span> 
              Modifying protocol ports or addresses may require a full server restart to take effect.
            </div>
          </div>
        )}

        {/* Path defaults */}
        {activeSection === 'defaults' && (
          <div className="card p-6 animate-in fade-in slide-in-from-bottom-2 duration-300">
             <div className="flex items-center justify-between mb-6 pb-4 border-b border-[var(--border)]">
              <div>
                <h2 className="text-lg font-medium">Path Defaults</h2>
                <p className="text-xs text-[var(--text-dim)]">These settings apply to all cameras unless specifically overridden in the camera configuration.</p>
              </div>
              <div className="flex items-center gap-3">
                <button
                  className="btn bg-[var(--bg)] hover:bg-[var(--bg-hover)] border border-[var(--border)]"
                  onClick={() => setPdDraft(JSON.stringify(pdCfg ?? {}, null, 2))}
                  disabled={loading}
                ><RotateCcw size={14} className="mr-2" /> Revert</button>
                <button
                  className="btn btn-primary min-w-[100px]"
                  disabled={!canAdmin || !pdValid || pdSaving}
                  onClick={savePathDefaults}
                >{pdSaving ? 'Saving…' : 'Save Changes'}</button>
              </div>
            </div>

            {viewMode === 'form' ? (
                <PathDefaultsForm config={currentPdObj} onChange={handlePdFormChange} disabled={loading || !canAdmin} />
            ) : (
                <>
                    <TextAreaJson value={pdDraft} onChange={setPdDraft} disabled={loading || !canAdmin} />
                    {!pdValid && <div className="mt-2 text-xs text-red-400 font-medium">⚠ Invalid JSON: Please fix syntax errors before saving.</div>}
                </>
            )}
             <div className="mt-6 text-xs text-[var(--text-dim)] bg-[var(--bg)] p-3 rounded border border-[var(--border)]">
              <span className="font-semibold text-blue-400 mr-1">Info:</span> 
              Changes to recording settings usually take effect immediately for new segments.
            </div>
          </div>
        )}

        {/* Active paths */}
        {activeSection === 'streams' && (
          <div className="card p-0 overflow-hidden animate-in fade-in slide-in-from-bottom-2 duration-300">
            <div className="flex items-center justify-between p-4 border-b border-[var(--border)] bg-[var(--bg-sub)]">
              <div>
                <h2 className="text-lg font-medium">Active Streams</h2>
                <p className="text-xs text-[var(--text-dim)]">Real-time status of all active media paths.</p>
              </div>
              <button 
                className="btn bg-white/5 hover:bg-white/10 border border-white/10" 
                onClick={refreshPaths} 
                disabled={pathsLoading}
              >
                <RotateCcw className={`w-3.5 h-3.5 mr-2 ${pathsLoading ? 'animate-spin' : ''}`} />
                {pathsLoading ? 'Refreshing…' : 'Refresh List'}
              </button>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[var(--text-dim)] bg-[var(--bg)] border-b border-[var(--border)]">
                    <th className="py-3 pl-4 pr-2 font-medium">Path Name</th>
                    <th className="py-3 px-2 font-medium">Active Readers</th>
                    <th className="py-3 px-2 font-medium">Recording Status</th>
                    <th className="py-3 px-2 font-medium text-right pr-4">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border)]/50">
                  {paths.length === 0 && (
                    <tr><td className="py-8 text-center text-[var(--text-dim)]" colSpan={4}>No active streams found.</td></tr>
                  )}
                  {paths.map((p: any) => {
                    const name = p?.name || p?.path || '-'
                    const conf = p?.conf || {}
                    const readers = (p?.readers && Array.isArray(p.readers) ? p.readers.length : (p?.readers ?? 0)) as number
                    const rec = !!conf.record
                    // try extract camera id from name: prefix like cam-<id> or <prefix>-<id>
                    let camId: number | null = null
                    const m = String(name).match(/-(\d+)$/)
                    if (m) camId = Number(m[1])
                    return (
                      <tr key={name} className="hover:bg-[var(--bg-hover)] transition-colors">
                        <td className="py-3 pl-4 pr-2 font-mono text-xs text-blue-400">{name}</td>
                        <td className="py-3 px-2">
                          <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-500/10 text-blue-400 border border-blue-500/20">
                            {readers} Clients
                          </span>
                        </td>
                        <td className="py-3 px-2">
                           <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium border ${rec ? 'bg-green-500/10 text-green-400 border-green-500/20' : 'bg-gray-500/10 text-gray-400 border-gray-500/20'}`}>
                            {rec ? '● Recording' : '○ Idle'}
                          </span>
                        </td>
                        <td className="py-3 px-2 text-right pr-4">
                          {camId ? (
                            <button 
                              className={`btn text-xs py-1 px-3 ${rec ? 'bg-red-500/10 text-red-400 hover:bg-red-500/20 border-red-500/20' : 'bg-green-500/10 text-green-400 hover:bg-green-500/20 border-green-500/20'}`} 
                              disabled={!canAdmin} 
                              onClick={() => toggleRecording(camId!, !rec)}
                            >
                              {rec ? 'Stop Recording' : 'Start Recording'}
                            </button>
                          ) : (
                            <span className="text-[var(--text-dim)] text-xs italic">System Path</span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
