// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Pick a USB webcam plugged into the OpenNVR server. The list comes from
 * the host agent (lib/hostAgent.ts) and refreshes every few seconds, so a
 * webcam plugged in while the dialog is open appears by itself. Webcams
 * already feeding another camera are shown but cannot be picked.
 */

import { useEffect, useState } from 'react'
import { AlertTriangle, Loader2, Usb } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { useDateFormat } from '../i18n'
import { ffmpegInstallCommand, hostAgentInstallCommand, type HostAgentInfo } from '../lib/hostAgent'
import { WebcamList, WebcamOption, webcamLabel } from './WebcamOption'

const POLL_MS = 5000

export function useHostAgent(enabled = true): { info: HostAgentInfo | null; error: boolean } {
  const [info, setInfo] = useState<HostAgentInfo | null>(null)
  const [error, setError] = useState(false)
  useEffect(() => {
    if (!enabled) return
    let stop = false
    let timer: number | undefined
    const load = async () => {
      try {
        const res = await apiService.getHostAgent()
        if (!stop) { setInfo(res?.data ?? null); setError(false) }
      } catch {
        if (!stop) setError(true)
      }
      if (!stop) timer = window.setTimeout(load, POLL_MS)
    }
    void load()
    return () => { stop = true; if (timer) window.clearTimeout(timer) }
  }, [enabled])
  return { info, error }
}

function Code({ children }: { children: string }) {
  return <code className="block mt-1 px-2 py-1 text-xs font-mono bg-[var(--bg-2)] border border-[var(--border)] select-all break-all">{children}</code>
}

export function HostWebcamPicker({
  value,
  onChange,
  ownCameraId,
}: {
  value: string | null
  onChange: (deviceId: string, name: string) => void
  /** When editing a camera: its own webcam is pickable, not "in use". */
  ownCameraId?: number
}) {
  const { info, error } = useHostAgent()
  // Dates through the user's formatters, never toLocaleString directly
  // (tests/test_ui_date_localisation.py).
  const fmt = useDateFormat()

  if (!info && !error) {
    return <div className="flex items-center gap-2 text-sm text-[var(--text-dim)]"><Loader2 size={14} className="animate-spin" /> Looking for the host agent…</div>
  }
  if (error || !info) {
    return <div className="text-sm text-[var(--danger)]">Could not ask OpenNVR about the host agent.</div>
  }
  if (!info.connected) {
    return (
      <div className="p-3 border border-[var(--warn)] bg-[color-mix(in_oklab,var(--warn)_10%,transparent)] text-sm space-y-1">
        <div className="flex items-center gap-2 font-medium"><AlertTriangle size={14} className="text-[var(--warn)]" />
          The host agent is not running on the OpenNVR server
        </div>
        <div className="text-[var(--text-dim)]">
          It finds the webcams plugged into that machine and streams them. Install it once, in the OpenNVR folder there:
        </div>
        <Code>{hostAgentInstallCommand(info.os)}</Code>
        {info.installed && info.last_seen && (
          <div className="text-xs text-[var(--text-dim)]">Last seen {fmt.dateTime(info.last_seen * 1000)}.</div>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {!info.ffmpeg?.found && (
        <div className="p-2 border border-[var(--warn)] text-sm">
          <div className="flex items-center gap-2"><AlertTriangle size={14} className="text-[var(--warn)]" />
            ffmpeg is not installed on the OpenNVR server, so webcams cannot stream yet. Install it there:
          </div>
          <Code>{ffmpegInstallCommand(info.os)}</Code>
        </div>
      )}
      {info.webcams.length === 0 ? (
        <div className="text-sm text-[var(--text-dim)] flex items-center gap-2">
          <Usb size={14} /> No webcam is plugged into {info.hostname || 'the OpenNVR server'}. Plug one in; it appears here within a few seconds.
        </div>
      ) : (
        <WebcamList>
          {info.webcams.map((w) => (
            <WebcamOption
              key={w.id}
              icon={<Usb size={14} />}
              name={webcamLabel(w.name)}
              subtitle={`USB · on ${info.hostname || 'the OpenNVR server'}`}
              selected={value === w.id}
              takenBy={w.assigned_camera_id !== null && w.assigned_camera_id !== ownCameraId
                ? w.assigned_camera_name || `camera ${w.assigned_camera_id}` : null}
              onSelect={() => onChange(w.id, webcamLabel(w.name))}
            />
          ))}
        </WebcamList>
      )}
      <div className="text-[11px] text-[var(--text-dim)]">
        Plugged into {info.hostname || 'the OpenNVR server'}. A webcam plugged in now appears here within a few seconds.
      </div>
    </div>
  )
}
