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

import { ChevronLeft } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Badge, EmptyState, ErrorCard, Skeleton } from '../../components/ui'
import { apiService } from '../../lib/apiService'
import { CameraSettingsPanel } from './CameraSettings'

type Cam = {
  id: number
  name: string
  ip_address: string
  is_active?: boolean
  manufacturer?: string | null
  model?: string | null
}

/**
 * Settings > Camera-Config > Device Settings.
 *
 * Two steps by design: pick a camera, then configure it. Camera settings are
 * per-device and capability-dependent — a single flat page can't represent
 * "these settings, for this camera", and a dropdown hides the fleet. So the
 * list is the landing view and selecting one drills in.
 *
 * Everything shown for a camera is detected from the device itself: a Hikvision
 * exposes its native ISAPI areas, a Secureye its CGI areas, and any other ONVIF
 * camera the common baseline. Mixed-vendor fleets are the normal case.
 */
export function CameraDeviceConfig() {
  const [cameras, setCameras] = useState<Cam[]>([])
  const [selected, setSelected] = useState<Cam | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    setLoading(true)
    apiService
      // active_only=false so a temporarily-offline camera can still be
      // configured; the list endpoint answers {cameras: [...], total: n}.
      .getCameras({ limit: 200, active_only: false })
      .then((res: any) => {
        if (!alive) return
        const list: Cam[] = Array.isArray(res.data?.cameras)
          ? res.data.cameras
          : Array.isArray(res.data)
            ? res.data
            : []
        setCameras(list)
      })
      .catch(
        (e: any) =>
          alive && setError(e?.data?.detail || e?.message || 'Failed to load cameras')
      )
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [])

  if (loading) return <Skeleton className="h-40 w-full" />
  if (error) return <ErrorCard message={error} />
  if (!cameras.length)
    return (
      <EmptyState
        title="No cameras yet"
        description="Add a camera first — its available settings are detected from the device."
      />
    )

  // --- step 2: one camera selected ---
  if (selected) {
    return (
      <div className="space-y-4">
        <button
          onClick={() => setSelected(null)}
          className="flex items-center gap-1 text-sm text-[var(--text-dim)] hover:text-[var(--text)] transition-colors"
        >
          <ChevronLeft size={16} /> All cameras
        </button>
        <div>
          <h3 className="text-lg font-medium">{selected.name}</h3>
          <p className="text-xs text-[var(--text-dim)] font-mono">
            {selected.ip_address}
            {selected.manufacturer ? ` · ${selected.manufacturer}` : ''}
            {selected.model ? ` ${selected.model}` : ''}
          </p>
        </div>
        <CameraSettingsPanel key={selected.id} camera={selected} />
      </div>
    )
  }

  // --- step 1: pick a camera ---
  return (
    <div className="space-y-3">
      <p className="text-xs text-[var(--text-dim)]">
        Select a camera to configure the device itself. Available settings are
        detected per camera, so vendor-specific features appear only where the
        hardware supports them.
      </p>
      <div className="space-y-2">
        {cameras.map((c) => (
          <button
            key={c.id}
            onClick={() => setSelected(c)}
            className="w-full flex items-center justify-between gap-4 rounded border border-[var(--border)] bg-[var(--panel-2)]/40 px-4 py-3 text-left hover:bg-[var(--panel-2)] transition-colors"
          >
            <div className="min-w-0">
              <div className="text-sm">{c.name}</div>
              <div className="text-xs text-[var(--text-dim)] font-mono truncate">
                {c.ip_address}
                {c.manufacturer ? ` · ${c.manufacturer}` : ''}
                {c.model ? ` ${c.model}` : ''}
              </div>
            </div>
            <div className="shrink-0">
              {c.is_active === false ? (
                <Badge variant="neutral">Inactive</Badge>
              ) : (
                <Badge variant="success">Active</Badge>
              )}
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}
