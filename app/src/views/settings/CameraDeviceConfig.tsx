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
import { EmptyState, ErrorCard, Skeleton } from '../../components/ui'
import { apiService } from '../../lib/apiService'
import { CameraSettingsPanel } from './CameraSettings'

type Cam = {
  id: number
  name: string
  ip_address: string
  manufacturer?: string | null
  model?: string | null
}

/**
 * Settings > Camera Configuration.
 *
 * Pick any camera and configure the device itself. The tabs shown are driven by
 * what that specific camera supports — a Hikvision exposes its native ISAPI
 * areas, a Dahua/CP Plus its CGI areas, and any other ONVIF camera the common
 * baseline. Mixed-vendor fleets are the normal case, not a special one.
 *
 * Renders the same panel as the per-row settings modal (no duplicated UI).
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
      // active_only=false so a temporarily-inactive camera can still be
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
        setSelected((cur) => cur ?? list[0] ?? null)
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

  return (
    <div className="space-y-4">
      <label className="flex flex-col gap-1 max-w-md">
        <span className="text-xs text-[var(--text-dim)]">Camera</span>
        <select
          value={selected?.id ?? ''}
          onChange={(e) =>
            setSelected(
              cameras.find((c) => c.id === Number(e.target.value)) ?? null
            )
          }
          className="bg-[var(--panel-2)] border border-[var(--border)] rounded px-3 py-2 text-sm"
        >
          {cameras.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name} — {c.ip_address}
              {c.manufacturer ? ` (${c.manufacturer}${c.model ? ` ${c.model}` : ''})` : ''}
            </option>
          ))}
        </select>
      </label>

      <p className="text-xs text-[var(--text-dim)]">
        The tabs below are detected from the selected camera — vendor-specific
        features appear only on cameras that support them, and the rest fall
        back to the common ONVIF settings.
      </p>

      {selected && (
        <div className="border-t border-[var(--border)] pt-4">
          {/* key forces a clean remount (and re-probe) when the camera changes */}
          <CameraSettingsPanel key={selected.id} camera={selected} />
        </div>
      )}
    </div>
  )
}
