// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiService } from '../lib/apiService'
import { addAlert, expire, matchAlert, type ActiveAlert, type BusEvent } from '../lib/tourAlerts'
import type { TourAlertSettings } from '../services/tourService'

/**
 * The cameras an alarm has brought to the front of a running tour
 * (lib/tourAlerts.ts decides which). Listens on its own /events/ws, v2,
 * filtered to the chosen sources, only while the tour's alerts are on;
 * the server already limits it to cameras this viewer may see.
 */
export function useTourAlerts(settings: TourAlertSettings | undefined, tourCameras: ReadonlySet<number>) {
  const [active, setActive] = useState<ActiveAlert[]>([])
  const lastEnded = useRef(new Map<number, number>())
  const settingsRef = useRef(settings)
  const camerasRef = useRef(tourCameras)
  settingsRef.current = settings
  camerasRef.current = tourCameras

  const on = !!settings?.enabled
  const sourcesKey = on ? [...(settings?.sources ?? [])].sort().join(',') : ''

  useEffect(() => {
    if (!on || !sourcesKey) return
    let ws: WebSocket | null = null
    let closed = false
    let retry: number | undefined
    let attempt = 0

    const connect = async () => {
      let ticket: string
      try {
        ticket = (await apiService.createEventsWsTicket()).data.ticket
      } catch {
        schedule()
        return
      }
      if (closed) return
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const types = sourcesKey.split(',').map((t) => `&types=${encodeURIComponent(t)}`).join('')
      ws = new WebSocket(`${proto}://${window.location.host}/api/v1/events/ws?ticket=${encodeURIComponent(ticket)}&v=2${types}`)
      ws.onopen = () => { attempt = 0 }
      ws.onmessage = (msg) => {
        const s = settingsRef.current
        if (!s) return
        let evt: BusEvent
        try { evt = JSON.parse(msg.data) } catch { return }
        const hit = matchAlert(evt, s, camerasRef.current)
        if (hit) setActive((cur) => addAlert(cur, hit, Date.now(), s, lastEnded.current))
      }
      ws.onclose = () => { if (!closed) schedule() }
    }
    const schedule = () => {
      if (closed) return
      attempt += 1
      retry = window.setTimeout(connect, Math.min(30_000, 1000 * 2 ** Math.min(attempt, 5)))
    }
    connect()
    return () => {
      closed = true
      window.clearTimeout(retry)
      if (ws) { ws.onclose = null; ws.close() }
    }
  }, [on, sourcesKey])

  // Run out what has been on screen long enough; remember when, for the cool-down.
  useEffect(() => {
    if (active.length === 0) return
    const id = window.setInterval(() => {
      setActive((cur) => {
        const now = Date.now()
        const { kept, ended } = expire(cur, now)
        if (ended.length === 0) return cur
        for (const a of ended) lastEnded.current.set(a.cameraId, now)
        return kept
      })
    }, 1000)
    return () => window.clearInterval(id)
  }, [active.length])

  // Alerts turned off for the tour: let go at once.
  useEffect(() => { if (!on) setActive([]) }, [on])

  const dismiss = useCallback((cameraId?: number) => {
    setActive((cur) => {
      const now = Date.now()
      const gone = cameraId == null ? cur : cur.filter((a) => a.cameraId === cameraId)
      for (const a of gone) lastEnded.current.set(a.cameraId, now)
      return cameraId == null ? [] : cur.filter((a) => a.cameraId !== cameraId)
    })
  }, [])

  return { active, dismiss }
}
