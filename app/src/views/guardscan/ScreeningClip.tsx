/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// The footage of one screening.
//
// The four photographs answer "who was this". Only the video answers
// "why was it 75%" — whether the guard rushed the back pass, whether
// the customer turned away, whether the wand never came off the hip.
// That is the question a manager opens a partial scan to ask.
//
// Nothing new is recorded for this. A screening already carries the
// camera and the exact instants it ran between, and the platform
// already serves a recorded time range as HLS, so this is a lookup.
//
// It is deliberately forgiving about having no video. Recordings are
// kept 30 days by default and screenings 90, so a screening outliving
// its footage is NORMAL, not a failure — and an operator who meets a
// red error for an expected state learns to distrust the page.

import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, Film } from 'lucide-react'
import { apiService } from '../../lib/apiService'
import { useSnackbar } from '../../components/Snackbar'
import { extractApiError } from '../../lib/apiError'
import { Button, Skeleton } from '../../components/ui'
import { VideoPlayer } from '../../components/VideoPlayer'
import type { Screening } from '../../services/guardScanService'

/** Seconds of run-up and run-out around the screening itself, so the
 *  first and last wand pass are not clipped at their own edges. */
const LEAD_S = 5
const TAIL_S = 5

type Session = {
  session_id?: string
  manifest_url?: string
  browser_mp4_url?: string
  needs_remux?: boolean
}

function windowFor(s: Screening): { start: string; end: string; seconds: number } | null {
  if (!s.ended_at) return null
  const end = new Date(s.ended_at).getTime()
  // started_at is derived server-side from the duration; fall back to
  // the duration directly, and to a minute if even that is missing.
  const start = s.started_at
    ? new Date(s.started_at).getTime()
    : end - (s.duration_s ?? 60) * 1000
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null
  const from = start - LEAD_S * 1000
  const to = end + TAIL_S * 1000
  return {
    start: new Date(from).toISOString(),
    end: new Date(to).toISOString(),
    seconds: (to - from) / 1000,
  }
}

export function ScreeningClip({ screening }: { screening: Screening }) {
  const { showError } = useSnackbar()
  const [exporting, setExporting] = useState(false)
  const range = windowFor(screening)
  const cameraId = screening.camera_id
  const playable = cameraId != null && range != null

  const session = useQuery({
    queryKey: ['screening-clip', screening.id],
    queryFn: async () => (await apiService.createHlsPlaybackSession({
      camera_id: cameraId as number,
      start: range!.start,
      end: range!.end,
    })).data as Session,
    enabled: playable,
    // A missing recording is an ANSWER, not a flake — retrying it just
    // delays the honest message.
    retry: 0,
    staleTime: Infinity,
  })

  // Sessions live in the backend's memory, capped per user (24 at the
  // time of writing). Leaking one per screening an operator glances at
  // would lock them out of playback entirely, and the lockout would
  // look like "playback is broken" rather than "you have too many
  // sessions open".
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
      const stamp = (screening.ended_at ?? '').replace(/[:.]/g, '-').slice(0, 19)
      const { data } = await apiService.createClipExportTicket({
        camera_id: cameraId as number,
        start: range!.start,
        duration: range!.seconds,
        filename: `screening-${screening.id}-${stamp}.mp4`,
      })
      // The backend streams the cut from MediaMTX straight to disk, so
      // an <a> is the whole client side of this.
      const a = document.createElement('a')
      a.href = (data as any).download_url
      a.click()
    } catch (err) {
      showError(extractApiError(err, 'Could not prepare the clip for download.'))
    } finally {
      setExporting(false)
    }
  }

  const gone = session.isError
    && (session.error as any)?.response?.status === 404

  return (
    <div className="mt-4 border-t border-[var(--border)] pt-3">
      <div className="mb-2 flex items-center gap-2">
        <Film size={13} className="text-[var(--text-dim)]" />
        <span className="text-[11px] font-medium">Footage of this screening</span>
        {playable && !gone && !session.isError && (
          <Button variant="outline" size="sm" className="ml-auto"
                  onClick={download} disabled={exporting}>
            <Download size={13} /> {exporting ? 'Preparing…' : 'Download'}
          </Button>
        )}
      </div>

      {!playable ? (
        <Note>
          {cameraId == null
            ? 'This screening is not tied to a camera core knows, so its footage cannot be found.'
            : 'This screening has no usable time range.'}
        </Note>
      ) : session.isPending ? (
        <Skeleton className="h-48" />
      ) : gone ? (
        <Note>
          The video for this screening is no longer kept. Recordings are held for a
          shorter period than the screening record itself.
        </Note>
      ) : session.isError ? (
        <Note>{extractApiError(session.error, 'Could not open the footage.')}</Note>
      ) : session.data?.needs_remux && session.data.browser_mp4_url ? (
        <VideoPlayer
          mode="playback"
          mp4Url={session.data.browser_mp4_url}
          preferredPlaybackType="mp4"
          className="h-48 w-full"
        />
      ) : session.data?.manifest_url ? (
        <VideoPlayer
          mode="playback"
          hlsPlaybackUrl={session.data.manifest_url}
          className="h-48 w-full"
        />
      ) : (
        <Note>No playable footage was returned for this screening.</Note>
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
