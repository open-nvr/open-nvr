// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Review one alarm from an app's own page: its photo, its description
// and the footage around it, in the same viewer the Alarms page uses.
//
// An app's Recent feed names the alarm by the alert_id the app gave it.
// The inbox has no lookup by that id, so this asks for the app's alarms
// on that camera around that time (a handful) and picks the one with
// that id. The inbox re-checks the viewer's camera scope, as it does
// everywhere.

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AuthedImage } from '../AuthedImage'
import { alertsInboxService, type InboxAlert, type InboxPage } from '../../services/alertsInboxService'
import { AlarmEvidenceViewer, bestImageName } from './AlarmsTable'
import { AlertClip } from './AlertClip'
import { Modal } from '../Modal'
import { Skeleton } from '../ui'

/** How far around the feed's time to look: the feed's clock is the
 *  app's, the inbox's is core's, and the two may disagree a little. */
const SLACK_MS = 5 * 60_000

export function AlertReview({ sourceName, alertId, camera, at, onClose }: {
  sourceName: string
  alertId: string
  camera?: string | null
  at?: string | null
  onClose: () => void
}) {
  const atMs = at ? new Date(at).getTime() : NaN
  const found = useQuery({
    queryKey: ['alert-review', sourceName, alertId],
    queryFn: async () => {
      const { data } = await alertsInboxService.listInboxAlerts({
        source_name: sourceName,
        camera_id: camera ?? undefined,
        ...(Number.isFinite(atMs) ? {
          from: new Date(atMs - SLACK_MS).toISOString(),
          to: new Date(atMs + SLACK_MS).toISOString(),
        } : {}),
        limit: 100,
      })
      return ((data as InboxPage).alerts ?? []).find((a) => a.alert_id === alertId) ?? null
    },
    retry: 0,
    staleTime: 60_000,
  })

  const alert: InboxAlert | null | undefined = found.data
  if (alert) return <AlarmReviewer alert={alert} onClose={onClose} />
  return (
    <Modal open title="Review alarm" onClose={onClose} widthClassName="w-[720px]">
      {found.isPending ? (
        <Skeleton className="h-64" />
      ) : (
        <p className="text-sm text-[var(--text-dim)]">
          {found.isError
            ? 'Could not look this alarm up right now.'
            : 'This alarm is not in the alarm list: it may have been removed, it may be on a camera you cannot see, or it was raised before the app started naming its alarms.'}
        </p>
      )}
    </Modal>
  )
}

/** One alarm, reviewed: its photos when it has some (with the footage
 *  under them), else its description and the footage on their own. The
 *  alarm bell, the Alarms page and app pages all open this. */
export function AlarmReviewer({ alert, onClose }: { alert: InboxAlert; onClose: () => void }) {
  if (alert.images?.length) return <AlarmEvidenceViewer alert={alert} onClose={onClose} />
  return (
    <Modal open title={alert.title} onClose={onClose} widthClassName="w-[720px]">
      {alert.description && <p className="text-sm text-[var(--text-dim)]">{alert.description}</p>}
      <AlertClip alert={alert} />
    </Modal>
  )
}

/** A small photo of one of an app's alarms, by the alert_id the app gave
 *  it, for its Recent feed; a click opens the alarm's review. All lines
 *  of a feed share one inbox query (same key), refreshed with the feed,
 *  and each photo is fetched once and kept. */
export function AlarmThumb({ sourceName, alertId }: { sourceName: string; alertId: string }) {
  const [open, setOpen] = useState(false)
  const alarms = useQuery({
    queryKey: ['app-alarms', sourceName],
    queryFn: async () => {
      const { data } = await alertsInboxService.listInboxAlerts({ source_name: sourceName, limit: 50 })
      return (data as InboxPage).alerts ?? []
    },
    staleTime: 10_000,
    refetchInterval: 15_000,
    retry: 0,
  })
  const alert = alarms.data?.find((a) => a.alert_id === alertId)
  const name = alert ? bestImageName(alert.images ?? []) : null
  if (!alert || !name) {
    // Keeps the lines aligned while the photo is on its way, or when an
    // alarm has none.
    return <span className="h-8 w-12 shrink-0 rounded bg-[var(--panel)]" aria-hidden="true" />
  }
  return (
    <>
      <AuthedImage
        queryKey={['alert-image', alert.id, name]}
        fetchBlob={(signal) => alertsInboxService.getAlertImage(alert.id, name, signal)}
        alt={`Evidence for: ${alert.title}`}
        className="h-8 w-12 shrink-0 cursor-zoom-in rounded object-cover"
        onClick={() => setOpen(true)}
      />
      {open && <AlarmReviewer alert={alert} onClose={() => setOpen(false)} />}
    </>
  )
}
