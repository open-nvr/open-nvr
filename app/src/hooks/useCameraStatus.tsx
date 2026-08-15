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
  camera_id?: number
  payload?: {
    status?: string
    camera_name?: string
  }
}

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
  const { showWarning, showSuccess } = useSnackbar()
  const [state, setState] = useState<CameraStatusState>({ statuses: {}, versions: {} })

  // The snackbar setters are stable (useCallback in the provider), but keep
  // them behind a ref so the WS effect depends only on `token`.
  const toastRef = useRef({ showWarning, showSuccess })
  toastRef.current = { showWarning, showSuccess }

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
        if (evt.event_type !== 'camera_status') return
        handleEvent(evt)
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
      {children}
    </CameraStatusContext.Provider>
  )
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
