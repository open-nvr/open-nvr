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

/**
 * Where one object went, across cameras.
 *
 * A visit's track_id belongs to a single camera, so the store on its own
 * cannot answer "where did it go after the dock?". `GET
 * /api/v1/search/journey` answers it two ways and always says which: an
 * exact identity (the same plate or the same recognised face, straight
 * from a KAI-C adapter, no inference), or evidence (the learned camera
 * graph says where it could have gone and in what time, the descriptors
 * those skills wrote say which candidate fits).
 *
 * Which is why this panel is built the way it is. Every hop shows its
 * score, how it was reached, and the server's own reasons — all of them,
 * including the ones that argued against the hop. journey.py's third
 * rule is that "a route an operator cannot audit is not evidence, and
 * this is ultimately used to say where somebody was", and a panel that
 * rendered a tidy line of thumbnails without the reasoning would be
 * exactly the thing that rule forbids.
 *
 * The reasons are shown verbatim and unclassified. The client could
 * guess which ones are objections by matching on the server's phrasing,
 * and it would be wrong the first time that phrasing changed — a UI that
 * silently mislabels an objection as supporting evidence is worse than
 * one that shows the operator the whole list and lets them read it.
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ImageOff, MapPin, Route, TriangleAlert } from 'lucide-react'
import { api } from '../lib/api'
import { AuthedImage } from './AuthedImage'
import { Modal } from './Modal'
import { useTranslation } from '../i18n'
import { Badge, Button, EmptyState, Skeleton } from './ui'

export type JourneyHop = {
  event_id: number
  camera_id: number
  camera_name: string | null
  label: string | null
  started_at: string | null
  ended_at: string | null
  evidence_url: string | null
  anchor: { camera_id: number; at: string | null }
  transit_seconds: number
  score: number
  method: string
  why: string[]
}

export type JourneyResponse = {
  anchor: {
    event_id: number
    camera_id: number
    camera_name: string | null
    label: string | null
    started_at: string | null
    evidence_url: string | null
  }
  method: string
  caveat: string
  hops: JourneyHop[]
}

/** How far ahead to look. A dead end is often just too short a window,
 *  and re-running with a longer one is the operator's own next thought —
 *  so it is a control here rather than a reason to close the panel. */
const WINDOWS = [15, 30, 60, 120]

/** An identity is not an inference and must not look like one. Evidence
 *  and time-only are both guesses, and time-only is the weakest answer
 *  the feature can give — the badge says so before the score does. */
const METHOD_VARIANT: Record<string, 'success' | 'info' | 'warning' | 'neutral'> = {
  identity: 'success',
  evidence: 'info',
  'time-only': 'warning',
  none: 'neutral',
}

/** The bar takes the method's colour too. A 41% time-only hop drawn in
 *  the same confident accent as a 71% inferred one says with colour what
 *  the badge just denied in words, and colour is read first. */
const METHOD_BAR: Record<string, string> = {
  identity: 'var(--badge-success-text)',
  evidence: 'var(--accent)',
  'time-only': 'var(--badge-warning-text)',
}

function clockTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function transit(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return s ? `${m}m ${s}s` : `${m}m`
}

function playbackHref(cameraId: number, at: string | null): string {
  return at
    ? `/playback/sync?camera=${cameraId}&at=${encodeURIComponent(at)}`
    : '/playback/sync'
}

/* ---------------------------- Panel ----------------------------- */

export function JourneyPanel(
  { eventId, onClose }: { eventId: number | null; onClose: () => void },
) {
  const { t } = useTranslation()
  const [windowMinutes, setWindowMinutes] = useState(30)

  const { data, isLoading, isError, error } = useQuery<JourneyResponse>({
    queryKey: ['journey', eventId, windowMinutes],
    enabled: eventId != null,
    queryFn: async () => {
      const res = await api.get(
        `/api/v1/search/journey?event_id=${eventId}&window_minutes=${windowMinutes}`)
      return res.data
    },
  })

  // The api client is a fetch wrapper, not axios: it throws an Error with
  // `status` on it. Reading `error.response.status` here compiled fine and
  // reported every 404 as a transient failure — "try again in a moment"
  // for a visit retention had already deleted.
  const status = (error as { status?: number } | null)?.status

  return (
    <Modal
      open={eventId != null}
      onClose={onClose}
      placement="side"
      widthClassName="w-[560px]"
      title={
        <>
          <Route size={15} />
          {t('journey.title')}
        </>
      }
    >
      <div className="space-y-3">
        {/* The window control stays available while loading and after a
            dead end: re-running further ahead is the whole recovery. */}
        <div className="flex items-center gap-2 text-xs">
          <span className="text-[var(--text-dim)]">{t('journey.window')}</span>
          <div className="flex gap-1">
            {WINDOWS.map((w) => (
              <Button
                key={w}
                size="sm"
                variant={w === windowMinutes ? 'primary' : 'outline'}
                onClick={() => setWindowMinutes(w)}
                aria-pressed={w === windowMinutes}
              >
                {t('journey.minutes').replace('{n}', String(w))}
              </Button>
            ))}
          </div>
        </div>

        {isLoading && (
          <div className="space-y-2">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        )}

        {isError && (
          <EmptyState
            icon={<TriangleAlert size={18} />}
            title={status === 404 ? t('journey.goneTitle') : t('journey.failedTitle')}
            description={status === 404 ? t('journey.goneBody') : t('journey.failedBody')}
          />
        )}

        {data && (
          <>
            <div className="flex items-center gap-2">
              <Badge variant={METHOD_VARIANT[data.method] ?? 'neutral'}>
                {t(`journey.method.${data.method}`)}
              </Badge>
              <span className="text-[11px] text-[var(--text-dim)]">
                {t('journey.hopCount').replace('{n}', String(data.hops.length))}
              </span>
            </div>

            {/* The server's own caveat, verbatim. It is the sentence that
                says how much weight this route can carry, and softening
                or dropping it would leave an operator trusting a guess. */}
            {data.caveat && (
              <p className="rounded border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1.5 text-[11px] text-[var(--text-dim)]">
                {data.caveat}
              </p>
            )}

            <ol className="space-y-2">
              <li>
                <StopCard
                  cameraId={data.anchor.camera_id}
                  cameraName={data.anchor.camera_name}
                  label={data.anchor.label}
                  at={data.anchor.started_at}
                  evidenceUrl={data.anchor.evidence_url}
                  eventId={data.anchor.event_id}
                  lead={t('journey.start')}
                />
              </li>
              {data.hops.map((hop, i) => (
                <li key={hop.event_id}>
                  <StopCard
                    cameraId={hop.camera_id}
                    cameraName={hop.camera_name}
                    label={hop.label}
                    at={hop.started_at}
                    evidenceUrl={hop.evidence_url}
                    eventId={hop.event_id}
                    lead={`${i + 1}`}
                    hop={hop}
                  />
                </li>
              ))}
            </ol>

            {data.hops.length === 0 && !data.caveat && (
              <EmptyState
                icon={<MapPin size={18} />}
                title={t('journey.deadEndTitle')}
                description={t('journey.deadEndBody')}
              />
            )}
          </>
        )}
      </div>
    </Modal>
  )
}

/* ---------------------------- Pieces ---------------------------- */

function StopCard({
  cameraId, cameraName, label, at, evidenceUrl, eventId, lead, hop,
}: {
  cameraId: number
  cameraName: string | null
  label: string | null
  at: string | null
  evidenceUrl: string | null
  eventId: number
  lead: string
  hop?: JourneyHop
}) {
  const { t } = useTranslation()
  return (
    <div className="rounded border border-[var(--border)] bg-[var(--panel)]">
      <div className="flex gap-2 p-2">
        <Link
          to={playbackHref(cameraId, at)}
          className="relative block h-16 w-24 shrink-0 overflow-hidden rounded bg-[var(--bg-2)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
          title={at ? t('journey.openAt').replace('{when}', new Date(at).toLocaleString()) : undefined}
        >
          {evidenceUrl ? (
            <AuthedImage
              queryKey={['journey-evidence', eventId]}
              fetchBlob={(signal) =>
                api.get(`/api/v1/events/${eventId}/evidence`, {
                  responseType: 'blob',
                  signal,
                })
              }
              alt={`${label ?? 'object'} on ${cameraName ?? cameraId}`}
              className="h-full w-full object-cover"
            />
          ) : (
            <span className="flex h-full w-full items-center justify-center text-[var(--text-dim)]">
              <ImageOff size={16} />
            </span>
          )}
        </Link>

        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex items-center gap-2 text-xs">
            <span className="text-[var(--text-dim)] tabular-nums">{lead}</span>
            <span className="truncate font-medium">{cameraName ?? `cam${cameraId}`}</span>
            <span className="ml-auto tabular-nums text-[var(--text-dim)]">{clockTime(at)}</span>
          </div>
          {hop ? (
            <>
              <div className="flex items-center gap-2 text-[10px] text-[var(--text-dim)]">
                <Badge variant={METHOD_VARIANT[hop.method] ?? 'neutral'}>
                  {t(`journey.method.${hop.method}`)}
                </Badge>
                <span className="tabular-nums">
                  {t('journey.after').replace('{t}', transit(hop.transit_seconds))}
                </span>
                <span
                  className="ml-auto tabular-nums"
                  title={t('journey.scoreHelp')}
                >
                  {Math.round(hop.score * 100)}%
                </span>
              </div>
              {/* A bar as well as the number: a column of percentages is
                  read as a ranking, and the point here is how far a hop
                  is from being worth believing at all. */}
              <div className="h-1 w-full overflow-hidden rounded bg-[var(--bg-2)]">
                <div
                  className="h-full"
                  style={{
                    width: `${Math.max(2, Math.min(100, hop.score * 100))}%`,
                    background: METHOD_BAR[hop.method] ?? 'var(--text-dim)',
                  }}
                />
              </div>
            </>
          ) : (
            <div className="text-[10px] text-[var(--text-dim)]">
              {label ?? t('journey.unknownLabel')}
            </div>
          )}
        </div>
      </div>

      {/* Every reason the server gave, including the ones against. */}
      {hop && hop.why.length > 0 && (
        <ul className="border-t border-[var(--border)] px-2 py-1.5 space-y-0.5">
          {hop.why.map((reason, i) => (
            <li key={i} className="text-[10px] leading-snug text-[var(--text-dim)]">
              {reason}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
