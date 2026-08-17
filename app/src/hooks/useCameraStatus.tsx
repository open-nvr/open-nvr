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

import { createContext, useContext, useEffect, useRef, useState, ReactNode } from 'react'
import { apiService } from '../lib/apiService'
import { useAuth } from '../auth/AuthContext'
import { useSnackbar } from '../components/Snackbar'

export type CameraConnectivity = 'online' | 'offline'

interface CameraStatusState {
  /** camera_id -> last known connectivity; absent = unknown */
  statuses: Record<number, CameraConnectivity>
  /**
   * camera_id -> counter bumped on every offline->online transition. Live
   * tiles key their player on this so a recovered camera gets a fresh
   * connection (and a fresh stream token) automatically.
   */
  versions: Record<number, number>
}

const CameraStatusContext = createContext<CameraStatusState>({ statuses: {}, versions: {} })

interface CameraStatusEvent {
  event_type?: string
  task?: string
  camera_id?: number
  timestamp?: number
  payload?: {
    status?: string
    camera_name?: string
    state?: string | null
    severity?: string
    description?: string
    [key: string]: unknown
  }
}

/** One live system-level alert (host resources / retention / recording). */
export interface SystemAlertEvent {
  /** Synthetic client id for list keys/dedup (timestamp + type). */
  id: string
  alert_type: string
  state: string | null
  severity: string
  description: string
  camera_id?: number
  camera_name?: string
  received_at: string
}

interface SystemHealthState {
  /** Newest-first ring buffer of alerts seen on this socket (max 100). */
  recentAlerts: SystemAlertEvent[]
  /** True while a critical disk condition is active (drives the banner). */
  diskCritical: boolean
}

const SystemHealthContext = createContext<SystemHealthState>({ recentAlerts: [], diskCritical: false })

// Alert types that flip the persistent disk banner rather than just toasting.
const DISK_CRITICAL_TYPES = new Set(['disk_low', 'disk_purge_exhausted'])
// Re-toasting the same still-active alert type is suppressed for this long
// (a WS reconnect or a re-published purge event must not spam the operator).
const TOAST_COOLDOWN_MS = 10 * 60 * 1000

/**
 * App-wide subscriber to the backend `camera_status` events (published when
 * MediaMTX reports a camera's stream ready/not-ready). Raises snackbar alerts
 * on every page and exposes per-camera state for the Live View tiles.
 *
 * WS lifecycle mirrors AIDetectionResults: single-use ticket per (re)connect,
 * exponential backoff capped at 60s, reset on successful open.
 */
export function CameraStatusProvider({ children }: { children: ReactNode }) {
  const { token } = useAuth()
  const { showWarning, showSuccess, showError } = useSnackbar()
  const [state, setState] = useState<CameraStatusState>({ statuses: {}, versions: {} })
  const [health, setHealth] = useState<SystemHealthState>({ recentAlerts: [], diskCritical: false })
  const lastToastAt = useRef<Record<string, number>>({})

  // The snackbar setters are stable (useCallback in the provider), but keep
  // them behind a ref so the WS effect depends only on `token`.
  const toastRef = useRef({ showWarning, showSuccess, showError })
  toastRef.current = { showWarning, showSuccess, showError }

  useEffect(() => {
    if (!token) return

    let ws: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let closedByUnmount = false
    let reconnectAttempt = 0
    const reconnectDelayMs = () =>
      Math.min(60000, 1000 * Math.pow(2, reconnectAttempt))

    const scheduleReconnect = () => {
      if (closedByUnmount) return
      const delay = reconnectDelayMs()
      reconnectAttempt += 1
      reconnectTimer = setTimeout(connect, delay)
    }

    const handleEvent = (evt: CameraStatusEvent) => {
      const cameraId = evt.camera_id
      const status = evt.payload?.status
      if (cameraId == null || (status !== 'online' && status !== 'offline')) return
      const name = evt.payload?.camera_name || `Camera ${cameraId}`

      setState(prev => {
        // Dedupe: the backend only publishes real transitions, but a WS
        // reconnect or duplicated delivery must not re-toast.
        if (prev.statuses[cameraId] === status) return prev

        if (status === 'offline') {
          toastRef.current.showWarning(`${name} is offline`)
          // Surface the drop even when the operator is on another tab.
          if (document.visibilityState !== 'visible' && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
            try { new Notification('Camera offline', { body: `${name} is offline` }) } catch { /* ignore */ }
          }
        } else {
          toastRef.current.showSuccess(`${name} is back online`)
        }

        return {
          statuses: { ...prev.statuses, [cameraId]: status },
          versions: status === 'online'
            ? { ...prev.versions, [cameraId]: (prev.versions[cameraId] || 0) + 1 }
            : prev.versions,
        }
      })
    }

    const handleSystemAlert = (evt: CameraStatusEvent) => {
      const alertType = evt.task || 'unknown'
      const p = evt.payload || {}
      const alertState = (p.state ?? null) as string | null
      const severity = (p.severity as string) || 'warning'
      const description = (p.description as string) ||
        (evt.event_type === 'camera_event'
          ? `Recording ${alertState === 'active' ? 'stalled' : 'resumed'} on ${p.camera_name || `camera ${evt.camera_id}`}`
          : alertType)

      const alert: SystemAlertEvent = {
        id: `${evt.timestamp ?? Date.now()}-${alertType}-${evt.camera_id ?? 'sys'}`,
        alert_type: alertType,
        state: alertState,
        severity,
        description,
        camera_id: evt.camera_id,
        camera_name: p.camera_name as string | undefined,
        received_at: new Date().toISOString(),
      }

      setHealth(prev => {
        let diskCritical = prev.diskCritical
        if (DISK_CRITICAL_TYPES.has(alertType)) {
          if (alertState === 'active' && severity === 'critical') diskCritical = true
          else if (alertState === 'inactive') diskCritical = false
        }
        return { recentAlerts: [alert, ...prev.recentAlerts].slice(0, 100), diskCritical }
      })

      // Toast with a per-(type,camera,state) cooldown: reconnects and
      // re-published discrete events (purges) must not spam.
      const key = `${alertType}:${evt.camera_id ?? ''}:${alertState ?? 'event'}`
      const nowMs = Date.now()
      if (nowMs - (lastToastAt.current[key] || 0) < TOAST_COOLDOWN_MS) return
      lastToastAt.current[key] = nowMs

      if (alertState === 'inactive') {
        toastRef.current.showSuccess(description)
      } else if (severity === 'critical') {
        toastRef.current.showError(description)
        // Critical host alerts (disk full) surface even from another tab.
        if (document.visibilityState !== 'visible' && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
          try { new Notification('OpenNVR system alert', { body: description }) } catch { /* ignore */ }
        }
      } else {
        toastRef.current.showWarning(description)
      }
    }

    const connect = async () => {
      let ticket: string
      try {
        const res = await apiService.createEventsWsTicket()
        ticket = res.data.ticket
      } catch {
        scheduleReconnect()
        return
      }
      if (closedByUnmount) return

      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const url = `${proto}://${window.location.host}/api/v1/events/ws?ticket=${encodeURIComponent(ticket)}`
      ws = new WebSocket(url)

      ws.onopen = () => {
        reconnectAttempt = 0
      }

      ws.onmessage = (msg) => {
        let evt: CameraStatusEvent
        try { evt = JSON.parse(msg.data) as CameraStatusEvent } catch { return }
        if (evt.event_type === 'camera_status') { handleEvent(evt); return }
        // Host alerts (disk/CPU/RAM/purge) + the recording watchdog's
        // camera-scoped stall events surface app-wide, same as camera drops.
        if (evt.event_type === 'system_alert' ||
            (evt.event_type === 'camera_event' && evt.task === 'recording_stalled')) {
          handleSystemAlert(evt)
        }
      }

      ws.onclose = () => {
        ws = null
        scheduleReconnect()
      }
    }

    connect()

    return () => {
      closedByUnmount = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      if (ws) {
        ws.onclose = null
        ws.close()
      }
    }
  }, [token])

  return (
    <CameraStatusContext.Provider value={state}>
      <SystemHealthContext.Provider value={health}>
        {children}
      </SystemHealthContext.Provider>
    </CameraStatusContext.Provider>
  )
}

/**
 * Live system-level alerts (host resources, retention purges, recording
 * stalls) from the shared events socket. `recentAlerts` is newest-first and
 * bounded; `diskCritical` drives the persistent disk banner.
 */
export function useSystemAlerts(): SystemHealthState {
  return useContext(SystemHealthContext)
}

/**
 * Connectivity of one camera as pushed by the backend.
 *
 * `status` is undefined until the first transition arrives (the provider does
 * not poll — unknown means "assume fine"). `version` increments on each
 * recovery; use it to key/restart players.
 */
export function useCameraStatus(cameraId?: number | null): {
  status: CameraConnectivity | undefined
  version: number
} {
  const { statuses, versions } = useContext(CameraStatusContext)
  if (cameraId == null) return { status: undefined, version: 0 }
  return { status: statuses[cameraId], version: versions[cameraId] || 0 }
}
