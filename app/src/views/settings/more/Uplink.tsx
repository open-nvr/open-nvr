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
import { apiService } from '../../../lib/apiService'
import { useAuth } from '../../../auth/AuthContext'

export function MoreUplink() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cfg, setCfg] = useState<any>({})

  const canAdmin = !!me?.is_superuser

  useEffect(() => {
    if (!canAdmin) return
    (async () => {
      try {
        setLoading(true)
        setError(null)
        const { data } = await apiService.getMediaSourceSettings()
        setCfg(data)
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load settings')
      } finally {
        setLoading(false)
      }
    })()
  }, [canAdmin])

  const save = async () => {
    if (!canAdmin) return
    try {
      setLoading(true)
      setError(null)
      await apiService.updateMediaSourceSettings({
        cloud_recording_server_ip: cfg.cloud_recording_server_ip || null,
        uplink_streaming_server_ip: cfg.uplink_streaming_server_ip || null,
      })
      const { data } = await apiService.getMediaSourceSettings()
      setCfg(data)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save')
    } finally {
      setLoading(false)
    }
  }

  if (!canAdmin) return <div className="text-sm text-amber-400">Admin only.</div>

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">Uplink Servers</h2>
        <button className="ml-auto px-2 py-1 bg-[var(--accent)] text-white" onClick={save} disabled={loading}>Save</button>
      </div>
      {error && <div className="text-sm text-red-400">{error}</div>}

      <div className="grid grid-cols-2 gap-3 text-sm">
        <div className="border border-neutral-700 bg-[var(--panel-2)] p-2 space-y-2">
          <div className="text-[var(--text-dim)]">Cloud Recording Server</div>
          <label className="flex items-center justify-between gap-2">
            <span>IP or Hostname</span>
            <input className="w-80 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.cloud_recording_server_ip||''}
              onChange={(e)=>setCfg({...cfg, cloud_recording_server_ip:e.target.value})} placeholder="e.g., 203.0.113.5 or recordings.example.com" />
          </label>
          <div className="text-[var(--text-dim)] text-xs">Used for offsite/cloud storage endpoints.</div>
        </div>

        <div className="border border-neutral-700 bg-[var(--panel-2)] p-2 space-y-2">
          <div className="text-[var(--text-dim)]">Streaming Server (uplink)</div>
          <label className="flex items-center justify-between gap-2">
            <span>IP or Hostname</span>
            <input className="w-80 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.uplink_streaming_server_ip||''}
              onChange={(e)=>setCfg({...cfg, uplink_streaming_server_ip:e.target.value})} placeholder="e.g., 198.51.100.10 or stream.example.com" />
          </label>
          <div className="text-[var(--text-dim)] text-xs">Used by Media Source to publish upstream.</div>
        </div>
      </div>
    </div>
  )
}


