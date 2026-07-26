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

import { useCallback, useEffect, useState } from 'react'
import { apiService } from '../lib/apiService'

export type CameraCapabilities = {
  camera_id: number
  driver_name: string | null
  manufacturer: string | null
  model: string | null
  firmware_version: string | null
  onvif_endpoints: Record<string, string>
  supported_areas: Record<string, boolean>
  capabilities: Record<string, unknown>
  probe_result: string
  probed_at: string | null
  probe_error: string | null
}

// Fetches a camera's device capabilities once (lazily probing the device on
// first open) and exposes a `reload(true)` to force a fresh probe. Drives which
// settings tabs/controls render.
export function useCameraCapabilities(cameraId: number | null, open: boolean) {
  const [caps, setCaps] = useState<CameraCapabilities | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(
    async (refresh = false) => {
      if (!cameraId) return
      setLoading(true)
      setError('')
      try {
        const res = refresh
          ? await apiService.refreshCameraCapabilities(cameraId)
          : await apiService.getCameraCapabilities(cameraId)
        setCaps(res.data)
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load camera capabilities')
      } finally {
        setLoading(false)
      }
    },
    [cameraId]
  )

  useEffect(() => {
    if (open && cameraId) load(false)
    if (!open) setCaps(null)
  }, [open, cameraId, load])

  return { caps, loading, error, reload: load }
}
