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
import { useAuth } from '../auth/AuthContext'

type OsInfo = { os?: string; kernel?: string; bios?: string; distro?: string; version?: string }
type UpdateStatus = { available?: boolean; count?: number; packages?: string[]; method?: string }
type AutoUpdateSettings = { enabled?: boolean; schedule?: string; reboot_if_required?: boolean }

export function FirmwareView() {
  const { user } = useAuth()
  const canAdmin = !!user?.is_superuser
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [osInfo, setOsInfo] = useState<OsInfo>({})
  const [updateStatus, setUpdateStatus] = useState<UpdateStatus>({})
  const [autoUpdate, setAutoUpdate] = useState<AutoUpdateSettings>({ enabled: true })

  useEffect(() => {
    ;(async () => {
      try {
        setLoading(true)
        setError(null)
        const [sysRes, updateRes, autoRes] = await Promise.allSettled([
          apiService.getSystemInfo(),
          apiService.getUpdateStatus(),
          apiService.getAutoUpdateSettings(),
        ])
        if (sysRes.status === 'fulfilled') setOsInfo(sysRes.value.data || {})
        if (updateRes.status === 'fulfilled') setUpdateStatus(updateRes.value.data || {})
        if (autoRes.status === 'fulfilled') setAutoUpdate(autoRes.value.data || { enabled: true })
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load firmware info')
      } finally { setLoading(false) }
    })()
  }, [])

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Firmware</h1>
      </div>

      {error && <div className="p-2 rounded bg-red-500/10 border border-red-500/30 text-red-300 text-sm">{error}</div>}

      {notice && <div className="p-2 rounded bg-green-500/10 border border-green-500/30 text-green-300 text-sm">{notice}</div>}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="border border-neutral-700 bg-[var(--panel-2)] p-3 text-sm space-y-1">
          <div className="text-[var(--text-dim)]">Operating System</div>
          <div className="text-[var(--text)]">{osInfo.os || '—'}</div>
          <div className="text-[var(--text-dim)]">Kernel</div>
          <div className="text-[var(--text)]">{osInfo.kernel || '—'}</div>
          <div className="text-[var(--text-dim)]">BIOS</div>
          <div className="text-[var(--text)]">{osInfo.bios || '—'}</div>
          <div className="text-[var(--text-dim)]">Distribution</div>
          <div className="text-[var(--text)]">{osInfo.distro || osInfo.version || '—'}</div>
        </div>

        <div className="border border-neutral-700 bg-[var(--panel-2)] p-3 text-sm space-y-2">
          <div className="text-[var(--text-dim)]">Security Updates</div>
          <div className="text-[var(--text)]">
            Status: {loading ? 'Checking…' : updateStatus.available ? `${updateStatus.count || 0} updates available` : 'Up to date'}
          </div>
          <div className="flex items-center gap-2 mt-2">
            <input 
              id="auto-update" 
              type="checkbox" 
              className="accent-[var(--accent)]" 
              checked={autoUpdate.enabled || false} 
              onChange={async (e) => {
                const newSettings = { ...autoUpdate, enabled: e.target.checked }
                setAutoUpdate(newSettings)
                try {
                  await apiService.updateAutoUpdateSettings(newSettings)
                  setNotice('Auto-update settings saved')
                } catch (err: any) {
                  setError(err?.data?.detail || 'Failed to save settings')
                }
              }} 
            />
            <label htmlFor="auto-update">Enable auto updates (Linux & Windows)</label>
          </div>
          <div className="text-xs text-[var(--text-dim)]">Auto updates are enabled by default as requested.</div>
          <div className="flex gap-2">
            <button 
              className="px-3 py-1 bg-[var(--panel)] border border-neutral-700 rounded disabled:opacity-50" 
              disabled={!canAdmin || loading}
              onClick={async () => {
                try {
                  setLoading(true)
                  const { data } = await apiService.checkUpdatesManual()
                  setUpdateStatus(data || {})
                  setNotice('Update check completed')
                } catch (err: any) {
                  setError(err?.data?.detail || 'Failed to check updates')
                } finally { setLoading(false) }
              }}
            >
              Check for updates
            </button>
            <button 
              className="px-3 py-1 bg-[var(--accent)] text-white rounded disabled:opacity-50" 
              disabled={!canAdmin || loading || !updateStatus.available}
              onClick={async () => {
                try {
                  setLoading(true)
                  const { data } = await apiService.applyUpdates()
                  setNotice(data?.status === 'success' ? 'Updates applied successfully' : 'Update failed')
                } catch (err: any) {
                  setError(err?.data?.detail || 'Failed to apply updates')
                } finally { setLoading(false) }
              }}
            >
              Apply updates
            </button>
          </div>
        </div>
      </div>
    </section>
  )
}


