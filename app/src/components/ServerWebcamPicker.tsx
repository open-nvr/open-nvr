// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Pick a USB webcam on the OpenNVR server, whichever way that server
 * streams one (#904):
 *
 *   - Linux with the webcam overlay: MediaMTX captures it itself; the list
 *     is GET /webcams/local (``LocalWebcamPicker``).
 *   - Linux without it: nothing to pick; say how to turn it on
 *     (``WebcamsOffNotice``). Never the host agent: on Linux its ffmpeg is
 *     not the answer, and it would tell the operator to install one.
 *   - Docker Desktop (macOS / Windows): the host agent (``HostWebcamPicker``).
 *
 * Add Camera and the camera edit dialog both decide this way; the edit
 * dialog used to offer the host agent on every OS.
 */

import { useEffect, useState } from 'react'
import { AlertTriangle, Usb } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { HostWebcamPicker } from './HostWebcamPicker'
import { WebcamList, WebcamOption } from './WebcamOption'

export type LocalWebcam = {
  id: string; name: string; node: string
  assigned_camera_id?: number | null; assigned_camera_name?: string | null
}

/** GET /api/v1/webcams/local, polled while the picker is open so a webcam
 *  plugged in during the dialog shows up. */
export type LocalWebcams = {
  loading: boolean; available: boolean; webcams: LocalWebcam[]
  /** The server's OS as its launcher wrote it (linux/darwin/windows), or null. */
  hostOs: string | null
}

export function useLocalWebcams(enabled: boolean): LocalWebcams {
  const [state, setState] = useState<LocalWebcams>({ loading: true, available: false, webcams: [], hostOs: null })
  useEffect(() => {
    if (!enabled) return
    let stop = false
    const tick = async () => {
      try {
        const { data } = await apiService.getLocalWebcams()
        if (!stop) setState({ loading: false, available: !!data?.available, webcams: data?.webcams ?? [], hostOs: data?.host_os ?? null })
      } catch {
        if (!stop) setState({ loading: false, available: false, webcams: [], hostOs: null })
      }
    }
    tick()
    const t = setInterval(tick, 4000)
    return () => { stop = true; clearInterval(t) }
  }, [enabled])
  return state
}

/** A Linux server with USB webcams switched off (WEBCAMS=off, or set but
 *  not applied since). The host agent is not the answer there — MediaMTX
 *  serves the webcam once WEBCAMS is on (#904). A server that merely has no
 *  webcam (a VM) does not land here: the overlay goes in anyway and the
 *  picker says "plug one in". */
export function WebcamsOffNotice() {
  return (
    <div className="p-3 border border-[var(--warn)] bg-[color-mix(in_oklab,var(--warn)_10%,transparent)] text-sm space-y-1">
      <div className="flex items-center gap-2 font-medium"><AlertTriangle size={14} className="text-[var(--warn)]" />
        USB webcams are off on the OpenNVR server
      </div>
      <div className="text-[var(--text-dim)]">
        Turn them on in the OpenNVR folder there: set this in <code className="font-mono">.env</code>, then run the second line.
      </div>
      <code className="block mt-1 px-2 py-1 text-xs font-mono bg-[var(--bg-2)] border border-[var(--border)] select-all break-all">WEBCAMS=on</code>
      <code className="block mt-1 px-2 py-1 text-xs font-mono bg-[var(--bg-2)] border border-[var(--border)] select-all break-all">./start.sh up</code>
      <div className="text-xs text-[var(--text-dim)]">
        Already set? Run <code className="font-mono">./start.sh up</code> once so it takes effect.
      </div>
    </div>
  )
}

export function LocalWebcamPicker({ webcams, value, onChange, ownCameraId }: {
  webcams: LocalWebcam[]; value: string | null; onChange: (id: string, name: string) => void
  /** When editing a camera: its own webcam is pickable, not "in use". */
  ownCameraId?: number
}) {
  if (webcams.length === 0) {
    return (
      <div className="text-sm text-[var(--text-dim)]">
        No USB webcam is plugged into the OpenNVR server right now. Plug one in; it appears here within a few seconds.
      </div>
    )
  }
  return (
    <WebcamList>
      {webcams.map((w) => (
        <WebcamOption
          key={w.id}
          icon={<Usb size={14} />}
          name={w.name}
          subtitle={`USB · ${w.node}`}
          selected={value === w.id}
          takenBy={w.assigned_camera_id && w.assigned_camera_id !== ownCameraId
            ? w.assigned_camera_name || `camera ${w.assigned_camera_id}` : null}
          onSelect={() => onChange(w.id, w.name)}
        />
      ))}
    </WebcamList>
  )
}

/** The whole decision, for a dialog that needs nothing else from the list
 *  (the camera edit dialog). Add Camera makes the same one inline, since it
 *  also uses the list for its preview and status. */
export function ServerWebcamPicker({ value, onChange, ownCameraId }: {
  value: string | null
  onChange: (deviceId: string, name: string) => void
  ownCameraId?: number
}) {
  const local = useLocalWebcams(true)
  if (local.loading) {
    return <div className="text-sm text-[var(--text-dim)]">Looking for webcams on the server…</div>
  }
  if (local.available) {
    return <LocalWebcamPicker webcams={local.webcams} value={value} onChange={onChange} ownCameraId={ownCameraId} />
  }
  if (local.hostOs === 'linux') return <WebcamsOffNotice />
  return <HostWebcamPicker value={value} onChange={onChange} ownCameraId={ownCameraId} />
}
