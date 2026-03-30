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

import React, { useCallback, useMemo, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { apiService } from '../lib/apiService'

type DiagItem = {
  key: string
  label: string
  status: 'idle' | 'ok' | 'warn' | 'error' | 'running'
  message?: string
  details?: any
}

export function Support() {
  const { user } = useAuth()
  const canAdmin = !!user?.is_superuser
  const [health, setHealth] = useState<any | null>(null)
  const [diag, setDiag] = useState<Record<string, DiagItem>>({})
  const [mtx, setMtx] = useState<{ global?: any; pathdefaults?: any; paths?: any } | null>(null)
  const [settings, setSettings] = useState<{ webrtc?: any; media_source?: any } | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const envApiBase = (import.meta as any)?.env?.VITE_API_BASE_URL as string | undefined
  const browserUa = typeof navigator !== 'undefined' ? navigator.userAgent : 'unknown'
  const appOrigin = typeof window !== 'undefined' ? window.location.origin : 'unknown'

  const resetDiag = useCallback(() => setDiag({}), [])

  const runDiagnostics = useCallback(async () => {
    resetDiag()
    setNotice(null)
    setError(null)
    const upd = (k: string, patch: Partial<DiagItem>) =>
      setDiag((d) => {
        const prev = d[k] || { key: k, label: k, status: 'idle' as DiagItem['status'] }
        const merged: DiagItem = { ...prev, ...patch }
        // Ensure required fields and canonical values
        merged.key = k
        if (!merged.label) merged.label = k
        if (!merged.status) merged.status = 'idle'
        return { ...d, [k]: merged }
      })

    // Health
    const kHealth = 'health'
    upd(kHealth, { label: 'API health', status: 'running' })
    try {
      const h = await apiService.getHealth()
      setHealth(h.data)
      upd(kHealth, { status: 'ok', message: `Service: ${h.data?.service || '-'} v${h.data?.version || '?'}`, details: h.data })
    } catch (e: any) {
      upd(kHealth, { status: 'error', message: e?.data?.detail || e?.message || 'Health check failed' })
    }

    // WebRTC client config
    const kRtc = 'rtc'
    upd(kRtc, { label: 'WebRTC client configuration', status: 'running' })
    try {
      const rtc = await apiService.getWebRTCClientConfig()
      setSettings((s) => ({ ...(s || {}), webrtc: rtc.data }))
      upd(kRtc, { status: 'ok', message: 'Fetched RTC config', details: rtc.data })
    } catch (e: any) {
      upd(kRtc, { status: 'warn', message: e?.data?.detail || 'RTC config not available' })
    }

    // Media Server (admin-only)
    if (canAdmin) {
      const kMtx = 'media_server'
      upd(kMtx, { label: 'Media Server admin API', status: 'running' })
      try {
        const [g, pd, pl] = await Promise.allSettled([
          apiService.mtxGlobalGet(),
          apiService.mtxPathdefaultsGet(),
          apiService.mtxPathsList(),
        ])
        const snap: any = {}
        if (g.status === 'fulfilled') snap.global = g.value.data?.details ?? g.value.data
        if (pd.status === 'fulfilled') snap.pathdefaults = pd.value.data?.details ?? pd.value.data
        if (pl.status === 'fulfilled') snap.paths = pl.value.data?.details ?? pl.value.data
        setMtx(snap)
        upd(kMtx, { status: 'ok', message: 'Media Server reachable', details: { keys: Object.keys(snap) } })
      } catch (e: any) {
        upd(kMtx, { status: 'warn', message: e?.data?.detail || e?.message || 'Media Server admin not reachable' })
      }

      // Admin settings
      const kMs = 'media_source'
      upd(kMs, { label: 'Media Source settings', status: 'running' })
      try {
        const ms = await apiService.getMediaSourceSettings()
        setSettings((s) => ({ ...(s || {}), media_source: ms.data }))
        upd(kMs, { status: 'ok', message: 'Fetched media source settings', details: ms.data })
      } catch (e: any) {
        upd(kMs, { status: 'warn', message: e?.data?.detail || 'Media source settings not available' })
      }
    }

    setNotice('Diagnostics completed')
  }, [canAdmin, resetDiag])

  const supportBundle = useMemo(() => {
    return {
      generated_at: new Date().toISOString(),
      location: typeof window !== 'undefined' ? window.location.href : undefined,
      app_origin: appOrigin,
      env_api_base: envApiBase || undefined,
      browser: browserUa,
      user: user ? { id: user.id, username: user.username, is_superuser: user.is_superuser } : null,
      health,
      diagnostics: Object.values(diag).map((d) => ({ key: d.key, label: d.label, status: d.status, message: d.message })),
      settings: settings || undefined,
      media_server: mtx || undefined,
    }
  }, [appOrigin, envApiBase, browserUa, user, health, diag, settings, mtx])

  const downloadBundle = useCallback(() => {
    try {
      const content = JSON.stringify(supportBundle, null, 2)
      const blob = new Blob([content], { type: 'application/json' })
      const ts = new Date().toISOString().replace(/[:.]/g, '-')
      const name = `opennvr-support-${ts}.json`
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = name
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      setNotice(`Downloaded ${name}`)
    } catch (e: any) {
      setError(e?.message || 'Failed to download support bundle')
    }
  }, [supportBundle])

  const copyLogInstructions = useCallback(async () => {
    const text = [
      'Server logs are written to logs/server.log with rotation.',
      'To summarize logs on the server host:',
      '  python server/logging_summary.py',
      '',
      'You can also inspect JSON logs with jq:',
      '  jq \'select(.level == "ERROR")\' logs/server.log',
    ].join('\n')
    try {
      await navigator.clipboard.writeText(text)
      setNotice('Copied log instructions to clipboard')
    } catch {
      setError('Failed to copy to clipboard')
    }
  }, [])

  const statusBadge = (s: DiagItem['status']) => {
    const map: Record<DiagItem['status'], string> = {
      idle: 'bg-[var(--bg)] text-[var(--text-dim)] border-[var(--border)]',
      running: 'bg-blue-500/10 text-blue-300 border-blue-500/30',
      ok: 'bg-green-500/10 text-green-300 border-green-500/30',
      warn: 'bg-amber-500/10 text-amber-300 border-amber-500/30',
      error: 'bg-red-500/10 text-red-300 border-red-500/30',
    }
    const label: Record<DiagItem['status'], string> = {
      idle: 'idle',
      running: 'running',
      ok: 'ok',
      warn: 'warn',
      error: 'error',
    }
    return <span className={`px-2 py-0.5 rounded text-xs border ${map[s]}`}>{label[s]}</span>
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Support</h1>
          <p className="text-[var(--text-dim)]">Diagnostics, environment info, and tools to help troubleshoot.</p>
        </div>
        <div className="text-xs text-[var(--text-dim)]">{health?.version ? <>API v{health.version}</> : '—'}</div>
      </div>

      {!canAdmin && (
        <div className="p-2 rounded bg-amber-500/10 border border-amber-500/30 text-amber-300 text-sm">
          Limited access. Ask an administrator to run full diagnostics.
        </div>
      )}

      {notice && (
        <div className="p-2 rounded bg-green-500/10 border border-green-500/30 text-green-300 text-sm">{notice}</div>
      )}
      {error && (
        <div className="p-2 rounded bg-red-500/10 border border-red-500/30 text-red-300 text-sm">{error}</div>
      )}

      {/* System info */}
  <div className="card">
        <h2 className="font-medium mb-2">System Information</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-sm">
          <div className="flex items-center justify-between p-2 bg-[var(--bg)] rounded border border-[var(--border)]">
            <span className="text-[var(--text-dim)]">API Base</span>
            <span className="font-mono text-xs">{envApiBase || appOrigin}</span>
          </div>
          <div className="flex items-center justify-between p-2 bg-[var(--bg)] rounded border border-[var(--border)]">
            <span className="text-[var(--text-dim)]">Service</span>
            <span className="font-mono text-xs">{health?.service || '-'}</span>
          </div>
          <div className="flex items-center justify-between p-2 bg-[var(--bg)] rounded border border-[var(--border)]">
            <span className="text-[var(--text-dim)]">Version</span>
            <span className="font-mono text-xs">{health?.version || '-'}</span>
          </div>
          <div className="flex items-center justify-between p-2 bg-[var(--bg)] rounded border border-[var(--border)]">
            <span className="text-[var(--text-dim)]">User</span>
            <span className="font-mono text-xs">{user ? `${user.username} (${user.is_superuser ? 'admin' : 'user'})` : '-'}</span>
          </div>
          <div className="flex items-center justify-between p-2 bg-[var(--bg)] rounded border border-[var(--border)]">
            <span className="text-[var(--text-dim)]">Browser</span>
            <span className="font-mono text-xs truncate max-w-[60%]" title={browserUa}>{browserUa}</span>
          </div>
          <div className="flex items-center justify-between p-2 bg-[var(--bg)] rounded border border-[var(--border)]">
            <span className="text-[var(--text-dim)]">Time</span>
            <span className="font-mono text-xs">{new Date().toLocaleString()}</span>
          </div>
        </div>
      </div>

      {/* Diagnostics */}
  <div className="card">
        <div className="flex items-center justify-between mb-2">
          <h2 className="font-medium">Quick Diagnostics</h2>
          <div className="flex items-center gap-2">
            <button className="btn" onClick={() => setHealth(null)}>Reset</button>
            <button className="btn btn-primary" onClick={runDiagnostics}>Run</button>
          </div>
        </div>
        <div className="space-y-2 text-sm">
          {Object.values(diag).length === 0 && (
            <div className="text-[var(--text-dim)]">No tests run yet.</div>
          )}
          {Object.values(diag).map((d) => (
            <div key={d.key} className="flex items-center justify-between p-2 bg-[var(--bg)] rounded border border-[var(--border)]">
              <div className="flex items-center gap-2">
                {statusBadge(d.status)}
                <span>{d.label}</span>
              </div>
              <div className="text-xs text-[var(--text-dim)]">{d.message || ''}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Media Server snapshot (admin) */}
      {canAdmin && (
        <div className="card">
          <div className="flex items-center justify-between mb-2">
            <h2 className="font-medium">Media Server Snapshot</h2>
            <button className="btn" onClick={runDiagnostics}>Refresh</button>
          </div>
          <div className="text-xs grid grid-cols-1 md:grid-cols-3 gap-2">
            <div className="p-2 bg-[var(--bg)] rounded border border-[var(--border)]">
              <div className="font-medium mb-1">Global</div>
              <pre className="overflow-auto max-h-48 whitespace-pre-wrap break-words">{mtx?.global ? JSON.stringify(mtx.global, null, 2) : '—'}</pre>
            </div>
            <div className="p-2 bg-[var(--bg)] rounded border border-[var(--border)]">
              <div className="font-medium mb-1">Path Defaults</div>
              <pre className="overflow-auto max-h-48 whitespace-pre-wrap break-words">{mtx?.pathdefaults ? JSON.stringify(mtx.pathdefaults, null, 2) : '—'}</pre>
            </div>
            <div className="p-2 bg-[var(--bg)] rounded border border-[var(--border)]">
              <div className="font-medium mb-1">Active Paths</div>
              <pre className="overflow-auto max-h-48 whitespace-pre-wrap break-words">{mtx?.paths ? JSON.stringify(mtx.paths, null, 2) : '—'}</pre>
            </div>
          </div>
        </div>
      )}

      {/* Tools */}
  <div className="card">
        <div className="flex items-center justify-between mb-2">
          <h2 className="font-medium">Tools</h2>
          <button className="btn btn-primary" onClick={downloadBundle}>Export Support Bundle</button>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-sm">
          <div className="p-2 bg-[var(--bg)] rounded border border-[var(--border)]">
            <div className="font-medium mb-1">Log collection</div>
            <p className="text-[var(--text-dim)]">Server logs are stored on the backend host at logs/server.log with rotation. Use your preferred method to collect the file when opening a support ticket.</p>
            <div className="mt-2 flex gap-2">
              <button className="btn" onClick={copyLogInstructions}>Copy instructions</button>
            </div>
          </div>
          <div className="p-2 bg-[var(--bg)] rounded border border-[var(--border)]">
            <div className="font-medium mb-1">Documentation</div>
            <ul className="list-disc list-inside text-[var(--text-dim)]">
              <li>Updates & Patching: see the Updates page for Media Server config</li>
              <li>AI Engine and Integrations: settings saved locally until backend is wired</li>
              <li>Health endpoint: GET /health</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  )
}
