// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The header's reminder that this tab is publishing a webcam
 * (lib/webcamSessions): which camera, how it is doing, a way back to its
 * page and a Stop. Renders nothing when no webcam is being published.
 */

import { Link } from 'react-router-dom'
import { Square } from 'lucide-react'
import { stopSession, useWebcamSessions, type WebcamSession } from '../lib/webcamSessions'

function dot(s: WebcamSession): { color: string; label: string } {
  switch (s.state.kind) {
    case 'live': return { color: 'var(--danger)', label: 'Publishing' }
    case 'connecting': return { color: 'var(--warn)', label: 'Connecting' }
    case 'retrying': return { color: 'var(--warn)', label: 'Reconnecting' }
    case 'error': return { color: 'var(--text-dim)', label: 'Stopped' }
    default: return { color: 'var(--text-dim)', label: 'Not publishing' }
  }
}

export function WebcamPublishingIndicator() {
  const sessions = useWebcamSessions()
  if (sessions.length === 0) return null
  return (
    <div className="flex items-center gap-1 mr-1">
      {sessions.map((s) => {
        const d = dot(s)
        return (
          <div
            key={s.cameraId}
            className="inline-flex items-center border border-[var(--border)] bg-[var(--panel)] text-xs h-7"
            title={`${d.label}: this tab's webcam into ${s.cameraName}. Closing or reloading the tab stops it.`}
          >
            <Link to={`/cameras/${s.cameraId}/publish`} className="inline-flex items-center gap-1.5 px-2 h-full hover:bg-[var(--panel-2)]">
              <span
                className={`w-2 h-2 rounded-full ${s.state.kind === 'live' ? 'animate-pulse' : ''}`}
                style={{ background: d.color }}
                aria-hidden="true"
              />
              <span className="max-w-[10rem] truncate max-sm:hidden">{s.cameraName}</span>
              <span className="sr-only">{d.label}</span>
            </Link>
            <button
              type="button"
              onClick={() => stopSession(s.cameraId)}
              className="px-1.5 h-full border-l border-[var(--border)] text-[var(--text-dim)] hover:text-[var(--danger)]"
              aria-label={`Stop publishing to ${s.cameraName}`}
              title="Stop publishing"
            >
              <Square size={11} />
            </button>
          </div>
        )
      })}
    </div>
  )
}
