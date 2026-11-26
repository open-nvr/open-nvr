// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The recording around an alarm, under its photo: "what happened here"
// should have one answer, not a still in one place and the footage in
// Recordings, to be found by hand at the right minute.
//
// The same mechanics as guard scan's screening clip (views/guardscan/
// ScreeningClip.tsx): an HLS playback session for a time window, deleted
// on close because sessions are capped per user, and a download of the
// same window.

import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, Film } from 'lucide-react'
import { apiService } from '../../lib/apiService'
import { useSnackbar } from '../Snackbar'
import { extractApiError } from '../../lib/apiError'
import { Button, Skeleton } from '../ui'
import { VideoPlayer } from '../VideoPlayer'
import type { InboxAlert } from '../../services/alertsInboxService'
import { alarmSeenIso } from '../../services/alertsInboxService'

/** Seconds before the alarm: most alarms fire AFTER what caused them
 *  (a missing bar is raised 25 s after it was last seen), so the lead
 *  is the long side. */
const LEAD_S = 45
const TAIL_S = 15

type Session = {
  session_id?: string
  manifest_url?: string
  browser_mp4_url?: string
  needs_remux?: boolean
}

/** "cam2" / "cam-2" / "2" → 2. */
function cameraNumber(handle: string | null | undefined): number | null {
  const m = /^(?:cam-?)?(\d+)$/i.exec(String(handle ?? '').trim())
  return m ? Number(m[1]) : null
}

export function AlertClip({ alert }: { alert: InboxAlert }) {
  const { showError } = useSnackbar()
  const [exporting, setExporting] = useState(false)
  const cameraId = cameraNumber(alert.camera_id)
  const at = alarmSeenIso(alert)
  const atMs = at ? new Date(at).getTime() : NaN
  const playable = cameraId != null && Number.isFinite(atMs)
  const start = new Date(atMs - LEAD_S * 1000).toISOString()
  const end = new Date(atMs + TAIL_S * 1000).toISOString()
  // The recording of the last seconds is still being written: a fresh
  // alarm's window is not playable yet, and saying so beats a bare 404.
  const tooFresh = Date.now() < atMs + TAIL_S * 1000 + 60_000

  const session = useQuery({
    queryKey: ['alert-clip', alert.id],
    queryFn: async () => (await apiService.createHlsPlaybackSession({
      camera_id: cameraId as number, start, end,
    })).data as Session,
    enabled: playable,
    // A missing recording is an ANSWER, not a flake.
    retry: 0,
    staleTime: Infinity,
  })

  const sessionIdRef = useRef<string | null>(null)
  sessionIdRef.current = session.data?.session_id ?? sessionIdRef.current
  useEffect(() => () => {
    const id = sessionIdRef.current
    if (id) apiService.deleteHlsPlaybackSession(id).catch(() => {})
  }, [])

  const download = async () => {
    if (!playable) return
    setExporting(true)
    try {
      const stamp = (at ?? '').replace(/[:.]/g, '-').slice(0, 19)
      const { data } = await apiService.createClipExportTicket({
        camera_id: cameraId as number,
        start,
        duration: LEAD_S + TAIL_S,
        filename: `alarm-${alert.id}-${stamp}.mp4`,
      })
      const a = document.createElement('a')
      a.href = (data as { download_url: string }).download_url
      a.click()
    } catch (err) {
      showError(extractApiError(err, 'Could not prepare the clip for download.'))
    } finally {
      setExporting(false)
    }
  }

  const status = (session.error as { response?: { status?: number } } | null)?.response?.status
  const gone = session.isError && status === 404

  return (
    <div className="mt-4 border-t border-[var(--border)] pt-3">
      <div className="mb-2 flex items-center gap-2">
        <Film size={13} className="text-[var(--text-dim)]" />
        <span className="text-[11px] font-medium">
          Footage: {LEAD_S} s before to {TAIL_S} s after the alarm
        </span>
        {playable && !session.isError && (
          <Button variant="outline" size="sm" className="ml-auto"
                  onClick={download} disabled={exporting}>
            <Download size={13} /> {exporting ? 'Preparing…' : 'Download'}
          </Button>
        )}
      </div>

      {!playable ? (
        <Note>
          {cameraId == null
            ? 'This alarm is not tied to a camera, so it has no footage.'
            : 'This alarm has no usable time, so its footage cannot be found.'}
        </Note>
      ) : session.isPending ? (
        <Skeleton className="h-64" />
      ) : gone && tooFresh ? (
        <Note>The recording of this moment is still being written. Try again in a minute.</Note>
      ) : gone ? (
        <Note>
          The video of this moment is no longer kept (recordings are held for a shorter
          time than alarms), or this camera was not recording then.
        </Note>
      ) : session.isError ? (
        <Note>{extractApiError(session.error, 'Could not open the footage.')}</Note>
      ) : session.data?.needs_remux && session.data.browser_mp4_url ? (
        <VideoPlayer mode="playback" mp4Url={session.data.browser_mp4_url}
                     preferredPlaybackType="mp4" className="h-64 w-full" />
      ) : session.data?.manifest_url ? (
        <VideoPlayer mode="playback" hlsPlaybackUrl={session.data.manifest_url}
                     className="h-64 w-full" />
      ) : (
        <Note>No playable footage was returned for this moment.</Note>
      )}
    </div>
  )
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded border border-[var(--border)] bg-[var(--panel)] px-3 py-4
                    text-center text-[11px] text-[var(--text-dim)]">
      {children}
    </div>
  )
}
