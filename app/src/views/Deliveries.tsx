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

// Deliveries — the first-class page for the package-delivery app.
//
// A "package detected" alert tells a homeowner a box exists. What they
// actually want to know, glancing at a phone or a front-desk screen, is:
// is anything waiting at the door right now, how long has it been there,
// who brought it, and did the person who just walked off with it live
// here. So this page is a row of doors, each with its live picture and
// the count on top, then a timeline of who brought and took what — not
// a list of alerts. The page stays quiet when nothing is wrong: the only
// thing that turns red is a package leaving with a stranger.

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, ArrowRight, BellOff, Camera as CameraIcon, CameraOff, Car, Clock, Eye,
  Package, PackageCheck, PackageOpen, PackageX, PenLine, RefreshCw, ScanSearch, Truck, User, UserX,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { apiService } from '../lib/apiService'
import { useTranslation } from '../i18n'
import {
  Badge, Button, Card, CardContent, CardHeader, CardTitle,
  EmptyState, PageHeader, Skeleton,
} from '../components/ui'
import { AuthedImage } from '../components/AuthedImage'
import { AlarmEvidenceViewer } from '../components/alarms/AlarmsTable'
import { useAlarmsList } from '../components/alarms/useAlarmsList'
import { alarmSeenIso, alertsInboxService, type InboxAlert } from '../services/alertsInboxService'
import type { RegisteredApp } from './AppCatalog'
import { AppNoCamerasBanner } from './apps/AppSetup'

export const DELIVERIES_CAPABILITY = 'deliveries'
const SOURCE = 'package-delivery'

export function findDeliveriesApp(apps: RegisteredApp[] | undefined): RegisteredApp | null {
  return (
    (apps ?? []).find(
      (a) => a.enabled && ((a.manifest as any)?.provides ?? []).includes(DELIVERIES_CAPABILITY)
    ) ?? null
  )
}

/* ------------------------- The app's contract ------------------------- */

type WhoKind = 'courier' | 'known' | 'owner' | 'stranger' | 'unknown' | 'operator'
type Who = { kind: WhoKind; name: string | null; reasons: string[] }
type EventKind = 'delivered' | 'picked_up' | 'taken' | 'reminder' | 'cleared' | 'false_alarm'
type Method = 'package_detection' | 'object_detection' | 'vqa' | 'tier0_proxy' | 'operator' | 'none'
type Vehicle = 'truck' | 'car' | 'van' | null
type DeliveryEvent = {
  id: string
  camera: string
  kind: EventKind
  time: number
  severity: 'info' | 'low' | 'high'
  who: Who
  count_before: number
  count_after: number
  method: Method
  confidence: number | null
  vehicle: Vehicle
  dwell_seconds: number | null
  acked: boolean
}
type DoorState = 'clear' | 'waiting' | 'reminder-due' | 'snoozed' | 'acknowledged'
type CheckReason = 'person-left' | 'scheduled' | 'action' | 'vehicle' | 'startup'
type CameraRow = {
  camera: string
  count: number
  present_since: number | null
  state: DoorState
  snoozed_until: number | null
  last_check: number | null
  last_check_reason: CheckReason | null
  next_reminder: number | null
  last_event: DeliveryEvent | null
  zone_drawn: boolean
  people_now: number
  vehicle_now: Vehicle
}
type DeliveriesState = {
  waiting_now?: number
  today?: { delivered: number; picked_up: number; taken: number; reminders: number; checks: number }
  counted_by?: {
    method: Method
    adapter: string | null
    task: string | null
    quality: 'good' | 'fair' | 'proxy' | 'none'
    /** The whole sentence — for the catalog view and the API. */
    note: string
    /** Just the timer clause, which is all this page is missing. */
    cadence?: string
  }
  hours?: { start: string; end: string } | null
  per_camera?: CameraRow[]
  events?: DeliveryEvent[]
  recent?: { message: string; time: number; level: string; camera?: string }[]
  needs_zone?: string[]
}
type Camera = { id: number; name: string }

/* ----------------------------- Helpers ------------------------------ */

function idFromHandle(handle: string): number | null {
  const m = /^cam-?(\d+)$/.exec(handle)
  return m ? Number(m[1]) : null
}

/** Unix seconds → "14:02". Always 24-hour, because the app's expected
 *  delivery hours arrive as "HH:MM" and the two must read the same way
 *  side by side. */
function hhmm(ts: number | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function ago(ts: number | null | undefined): string {
  if (!ts) return 'never'
  const age = Math.max(0, Date.now() / 1000 - ts)
  if (age < 60) return 'just now'
  if (age < 3600) return `${Math.floor(age / 60)} min ago`
  if (age < 86400) return `${Math.floor(age / 3600)} h ago`
  return `${Math.floor(age / 86400)} d ago`
}

/** 1320 → "22 min", 5400 → "1 h 30 min", 40 → "40 s". */
function span(s: number | null | undefined): string {
  const v = Math.max(0, Math.floor(s ?? 0))
  if (v < 60) return `${v} s`
  if (v < 3600) return `${Math.floor(v / 60)} min`
  const h = Math.floor(v / 3600)
  const m = Math.floor((v % 3600) / 60)
  return m ? `${h} h ${m} min` : `${h} h`
}

// One table per vocabulary, so the door card, the timeline dot and the
// stat row can never disagree about what "taken" looks like.
const KIND_UI: Record<EventKind, { label: string; color: string }> = {
  delivered:   { label: 'Delivered',    color: 'var(--accent)' },
  picked_up:   { label: 'Collected',    color: 'var(--ok)' },
  taken:       { label: 'Taken',        color: 'var(--danger)' },
  reminder:    { label: 'Reminder',     color: 'var(--text-dim)' },
  cleared:     { label: 'Cleared',      color: 'var(--text-dim)' },
  false_alarm: { label: 'Not a package', color: 'var(--text-dim)' },
}

const STATE_UI: Record<DoorState, {
  label: string
  variant: 'destructive' | 'warning' | 'neutral' | 'success' | 'info'
  hint: string
}> = {
  clear: {
    label: 'Clear', variant: 'success',
    hint: 'Nothing on the doorstep.',
  },
  waiting: {
    label: 'Waiting', variant: 'warning',
    hint: 'A package is sitting at the door. A reminder will fire if nobody collects it.',
  },
  'reminder-due': {
    label: 'Reminder due', variant: 'destructive',
    hint: 'Still waiting past the reminder delay — collect it, snooze, or acknowledge.',
  },
  snoozed: {
    label: 'Snoozed', variant: 'neutral',
    hint: 'Reminders paused until the time shown. The count is still live.',
  },
  acknowledged: {
    label: 'Acknowledged', variant: 'info',
    hint: 'An operator knows about these packages; reminders are off until the next delivery.',
  },
}

const WHO_LABEL: Record<WhoKind, string> = {
  courier: 'courier', known: 'known person', owner: 'resident', stranger: 'stranger',
  unknown: 'someone', operator: 'operator',
}

const METHOD_LABEL: Record<Method, string> = {
  package_detection: 'package detection',
  object_detection: 'object detection',
  vqa: 'visual question answering',
  tier0_proxy: 'COCO stand-in',
  operator: 'operator',
  none: 'no counter',
}

const CHECK_REASON: Record<CheckReason, string> = {
  'person-left': 'someone left the doorstep',
  scheduled: 'scheduled',
  action: 'you asked',
  vehicle: 'a vehicle stopped',
  startup: 'startup',
}

function VehicleIcon({ vehicle, size = 13 }: { vehicle: Vehicle; size?: number }) {
  if (!vehicle) return null
  return vehicle === 'car' ? <Car size={size} aria-hidden /> : <Truck size={size} aria-hidden />
}

/** "courier · van" / "Ravi" — the name when we have one, else the kind. */
function whoLabel(who: Who): string {
  return who.name ? who.name : WHO_LABEL[who.kind] ?? who.kind
}

/* ------------------------------ Page ------------------------------ */

export function Deliveries() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [viewing, setViewing] = useState<InboxAlert | null>(null)

  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
  })
  const app = findDeliveriesApp(appsQuery.data)

  const statusQuery = useQuery({
    queryKey: ['app-status', app?.id],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(app!.id)
      return data as { state?: DeliveriesState }
    },
    enabled: Boolean(app),
    retry: 0,
    // A door check takes a few seconds; an operator who pressed "Check
    // now" should see the count move without reloading.
    refetchInterval: 5000,
  })
  const state: DeliveriesState = statusQuery.data?.state ?? {}

  const camerasQuery = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const { data } = await apiService.getCameras()
      const list = Array.isArray(data) ? data : (data as any)?.cameras
      return (Array.isArray(list) ? list : []) as Camera[]
    },
    enabled: Boolean(app),
    retry: 0,
  })
  const cameraName = (handle: string) => {
    const id = idFromHandle(handle)
    return camerasQuery.data?.find((c) => c.id === id)?.name ?? handle
  }

  // One mutation, keyed by camera, so only the door being acted on greys
  // its buttons out — the other doors stay usable.
  const act = useMutation({
    mutationFn: async ({ action, params }: { action: string; params: Record<string, unknown> }) =>
      apiService.invokeAppAction(app!.id, action, params),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['app-status', app?.id] }),
  })
  const run = (action: string, params: Record<string, unknown>) => act.mutate({ action, params })
  const busyCamera = act.isPending ? (act.variables?.params.camera as string | undefined) : undefined

  const rows = state.per_camera ?? []
  const events = state.events ?? []
  const needsZone = state.needs_zone ?? []
  const today = state.today ?? { delivered: 0, picked_up: 0, taken: 0, reminders: 0, checks: 0 }
  const waiting = state.waiting_now ?? rows.reduce((n, r) => n + r.count, 0)
  const oldestSince = rows
    .map((r) => r.present_since)
    .filter((v): v is number => typeof v === 'number' && v > 0)
    .sort((a, b) => a - b)[0]

  // The alarms carry the photographs (before / after / person / scene);
  // the app's events carry the story. They are joined loosely — same
  // camera, within 90 s — because the alert is fired after the check
  // that produced the event finishes, and the two clocks are not the
  // same one. Ninety seconds is wider than any check takes and narrower
  // than two deliveries at the same door ever are.
  const alarms = useAlarmsList({
    queryKeyPrefix: 'deliveries-alarms', sourceName: SOURCE,
    unacked: false, severity: null, page: 1, pageSize: 50, skip: 0,
  })
  const alarmFor = useMemo(() => {
    const list = alarms.rows.filter((a) => a.source_name === SOURCE && a.images?.length)
    return (ev: DeliveryEvent): InboxAlert | undefined => {
      let best: InboxAlert | undefined
      let bestGap = 91
      for (const a of list) {
        if (a.camera_id !== ev.camera) continue
        const iso = alarmSeenIso(a)
        if (!iso) continue
        const gap = Math.abs(new Date(iso).getTime() / 1000 - ev.time)
        if (gap < bestGap) { best = a; bestGap = gap }
      }
      return best
    }
  }, [alarms.rows])

  if (!appsQuery.isPending && !app) {
    return (
      <section className="space-y-4">
        <PageHeader title={t('deliveries.title')} description={t('deliveries.description')} />
        <EmptyState
          icon={<Package size={28} />}
          title={t('deliveries.noApp')}
          description="Install and enable Package Delivery from the App Catalog. It watches the porch zone you draw on each door camera, counts the packages whenever someone walks away, tells a courier from a resident from a stranger, reminds you while a parcel sits outside, and records who took it."
        />
      </section>
    )
  }

  const counted = state.counted_by
  const hours = state.hours

  return (
    <section className="space-y-4">
      <PageHeader
        title={t('deliveries.title')}
        description={t('deliveries.description')}
        actions={
          <>
            <Button size="sm" variant="outline" onClick={() => statusQuery.refetch()}>
              <RefreshCw size={14} /> {t('deliveries.refresh')}
            </Button>
            {app && (
              <Link to={`/app-catalog/${app.id}`}>
                <Button size="sm" variant="outline"><PenLine size={14} /> {t('deliveries.drawZones')}</Button>
              </Link>
            )}
          </>
        }
      />

      <AppNoCamerasBanner app={app} />

      {/* ── Headline ── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat icon={<Package size={16} />} value={waiting} label={t('deliveries.waitingNow')}
              sub={oldestSince ? `since ${hhmm(oldestSince)}` : undefined}
              tone={waiting > 0 ? 'warn' : undefined} />
        <Stat icon={<PackageCheck size={16} />} value={today.delivered} label={t('deliveries.deliveredToday')} />
        <Stat icon={<PackageOpen size={16} />} value={today.picked_up} label={t('deliveries.collectedToday')} />
        <Stat icon={today.taken > 0 ? <UserX size={16} /> : <PackageX size={16} />} value={today.taken}
              label={t('deliveries.taken')} tone={today.taken > 0 ? 'danger' : undefined}
              title="Packages that left with someone we did not recognise" />
      </div>

      {/* ── How packages are counted ── */}
      {counted && (
        <CountedBy counted={counted} hours={hours ?? null} checks={today.checks} />
      )}

      {needsZone.length > 0 && app && (
        <div className="rounded border border-[var(--warn)]/40 bg-[var(--warn)]/5 px-3 py-2 text-sm flex items-start gap-2">
          <PenLine size={14} className="text-[var(--warn)] shrink-0 mt-1" />
          <span className="flex-1">
            {needsZone.length === 1 ? 'One door has no porch zone drawn:' : `${needsZone.length} doors have no porch zone drawn:`}{' '}
            <b>{needsZone.map(cameraName).join(', ')}</b>. The whole frame is counted there, so a plant pot or a
            doormat gets counted as a package. Draw the zone around the step where parcels are actually left.
          </span>
          <Link to={`/app-catalog/${app.id}`} className="shrink-0 text-[var(--accent)] underline">Draw it</Link>
        </div>
      )}

      {/* ── Doors ── */}
      {statusQuery.isPending ? (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          <Skeleton className="h-64" /><Skeleton className="h-64" /><Skeleton className="h-64" />
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<CameraIcon size={24} />}
          title="No door cameras selected"
          description="Pick the cameras that see your doorsteps and draw a porch zone on each (App Catalog → Package Delivery → Configure). Each door then appears here with its live picture and count."
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {rows.map((r) => (
            <DoorCard key={r.camera} row={r} name={cameraName(r.camera)}
                      busy={busyCamera === r.camera} run={run} />
          ))}
        </div>
      )}

      {/* ── Timeline ── */}
      <Card>
        <CardHeader>
          <Clock size={16} className="text-[var(--text-dim)]" />
          <CardTitle>{t('deliveries.timeline')}</CardTitle>
          <span className="text-xs text-[var(--text-dim)]">who brought and took what, newest first</span>
          {statusQuery.isFetching && <span className="ml-auto text-xs text-[var(--text-dim)]">updating…</span>}
        </CardHeader>
        <CardContent>
          {statusQuery.isPending ? (
            <Skeleton className="h-24" />
          ) : events.length === 0 ? (
            <div className="text-sm text-[var(--text-dim)] py-4 text-center">
              Nothing today. Deliveries appear here as soon as a courier leaves a parcel
              {hours ? ` — expected between ${hours.start} and ${hours.end}` : ''}.
            </div>
          ) : (
            <ol className="divide-y divide-[var(--border)]">
              {events.map((ev) => (
                <EventRow key={ev.id} ev={ev} name={cameraName(ev.camera)}
                          alert={alarmFor(ev)} onView={setViewing} />
              ))}
            </ol>
          )}
        </CardContent>
      </Card>

      {viewing && <AlarmEvidenceViewer alert={viewing} onClose={() => setViewing(null)} />}
    </section>
  )
}

/* ----------------------------- Pieces ----------------------------- */

function CountedBy({ counted, hours, checks }: {
  counted: NonNullable<DeliveriesState['counted_by']>
  hours: { start: string; end: string } | null
  checks: number
}) {
  const method = METHOD_LABEL[counted.method] ?? counted.method
  const engine = counted.adapter ?? counted.task
  const quality = counted.quality
  // The badge explains itself on hover; the text after it only says
  // what the app cannot know about itself (why proxy counting is weak).
  const QUALITY: Record<typeof quality, { label: string; variant: 'success' | 'neutral' | 'warning' | 'destructive'; hint: string }> = {
    good: { label: 'good', variant: 'success', hint: 'A model that knows what a package is.' },
    fair: { label: 'fair', variant: 'neutral', hint: 'Counts are usable but may miss small or odd-shaped parcels.' },
    proxy: {
      label: 'proxy', variant: 'warning',
      hint: 'COCO has no package class — counting bags and cases as a stand-in. Install a package-capable model for better results.',
    },
    none: { label: 'none', variant: 'destructive', hint: 'No skill can count packages on this box.' },
  }
  const q = QUALITY[quality] ?? QUALITY.none

  if (quality === 'none') {
    return (
      <div className="rounded border border-[var(--danger)]/40 bg-[var(--danger)]/5 px-3 py-2 text-sm flex flex-wrap items-center gap-2">
        <AlertTriangle size={14} className="text-[var(--danger)]" />
        <span>
          <b>No skill can count packages on this box.</b> Install a VQA or package-detection adapter
          (AI &amp; Detections → AI Adapters) — until then the doors below are watched but never counted.
        </span>
        <Link to="/ai-adapters" className="ml-auto text-[var(--accent)] underline">AI Adapters</Link>
      </div>
    )
  }
  return (
    <div className="text-sm text-[var(--text-dim)] flex items-start gap-2 px-1">
      <ScanSearch size={14} className="shrink-0 mt-1" />
      <p className="m-0">
        <Badge variant={q.variant} title={q.hint} className="mr-2 align-middle">{q.label}</Badge>
        Counted by <b className="text-[var(--text)]">{engine ?? method}</b>
        {engine ? ` (${method})` : ''} when someone leaves the doorstep
        {hours ? `, ${hours.start}–${hours.end}` : ''}.
        {/* The app's own CADENCE, so the page never claims an interval
            the operator has since changed — and not `note`, which is
            the whole sentence and printed this panel twice. */}
        {counted.cadence ? ` Checked ${counted.cadence}.` : ''}
        {checks > 0 ? ` ${checks} ${checks === 1 ? 'check' : 'checks'} today.` : ''}
        {quality === 'proxy' && (
          <span className="text-xs">
            {' '}COCO has no package class — counting bags and cases as a stand-in; install a package-capable model for better results.
          </span>
        )}
      </p>
    </div>
  )
}

/** One door: the live picture with the count on it, its state, and the
 *  actions that make sense in that state. */
function DoorCard({ row: r, name, busy, run }: {
  row: CameraRow
  name: string
  busy: boolean
  run: (action: string, params: Record<string, unknown>) => void
}) {
  const ui = STATE_UI[r.state] ?? STATE_UI.clear
  const camId = idFromHandle(r.camera)
  const hasPackages = r.count > 0
  const remindersOn = r.state === 'waiting' || r.state === 'reminder-due'
  const p = { camera: r.camera }
  const last = r.last_event
  return (
    <Card className={r.state === 'reminder-due' ? 'border-[var(--warn)]/60' : undefined}>
      <div className="relative">
        <Snapshot cameraId={camId} alt={`Live view of ${name}`} className="aspect-video w-full rounded-t" />
        {hasPackages && (
          <div className="absolute left-2 top-2 rounded-full bg-black/70 px-2.5 py-1 text-xs text-white flex items-center gap-1.5"
               aria-label={`${r.count} ${r.count === 1 ? 'package' : 'packages'} waiting`}>
            <Package size={13} aria-hidden />
            <b className="tabular-nums">{r.count}</b>
            {r.present_since && <span className="opacity-80">since {hhmm(r.present_since)}</span>}
          </div>
        )}
        {(r.people_now > 0 || r.vehicle_now) && (
          <div className="absolute right-2 top-2 flex items-center gap-1.5 rounded-full bg-black/70 px-2 py-1 text-xs text-white">
            {r.people_now > 0 && (
              <span className="flex items-center gap-0.5" title={`${r.people_now} at the door now`}
                    aria-label={`${r.people_now} ${r.people_now === 1 ? 'person' : 'people'} at the door now`}>
                <User size={13} aria-hidden />{r.people_now}
              </span>
            )}
            {r.vehicle_now && (
              <span title={`A ${r.vehicle_now} is outside now`} aria-label={`A ${r.vehicle_now} is outside now`}>
                <VehicleIcon vehicle={r.vehicle_now} />
              </span>
            )}
          </div>
        )}
      </div>
      <CardHeader>
        <CardTitle className="truncate">{name}</CardTitle>
        <Badge variant={ui.variant} title={ui.hint}>
          {ui.label}{r.state === 'snoozed' && r.snoozed_until ? ` until ${hhmm(r.snoozed_until)}` : ''}
        </Badge>
        {!r.zone_drawn && <Badge variant="warning" title="No porch zone — the whole frame is counted.">no zone</Badge>}
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="text-xs text-[var(--text-dim)]">
          last checked {ago(r.last_check)}
          {r.last_check_reason ? ` (${CHECK_REASON[r.last_check_reason] ?? r.last_check_reason})` : ''}
          {r.next_reminder && remindersOn ? ` · reminder at ${hhmm(r.next_reminder)}` : ''}
        </div>
        {last ? (
          <div className="text-sm flex items-center gap-1.5 min-w-0">
            <span className="h-2 w-2 rounded-full shrink-0" style={{ background: KIND_UI[last.kind]?.color }} aria-hidden />
            <span className="truncate">
              {KIND_UI[last.kind]?.label ?? last.kind} {hhmm(last.time)} by {whoLabel(last.who)}
              {last.vehicle ? ` · ${last.vehicle}` : ''}
              {last.kind === 'taken' && last.severity === 'high' ? ' · high' : ''}
            </span>
          </div>
        ) : (
          <div className="text-sm text-[var(--text-dim)]">No deliveries seen yet.</div>
        )}
        <div className="flex flex-wrap gap-1.5 pt-1">
          {hasPackages && (
            <Button size="sm" variant="primary" disabled={busy} onClick={() => run('picked_up', p)}
                    title="The package is gone or in your hands — records a collection and clears the count.">
              <PackageOpen size={14} /> Collected
            </Button>
          )}
          {hasPackages && (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => run('not_a_package', p)}
                    title="A plant pot, a doormat, a shoe — clears the count and teaches the counter.">
              <PackageX size={14} /> Not a package
            </Button>
          )}
          {remindersOn && (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => run('snooze', { ...p, minutes: 60 })}
                    title="Pause reminders for an hour; the count stays live.">
              <BellOff size={14} /> Snooze 1 h
            </Button>
          )}
          {remindersOn && (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => run('acknowledge', p)}
                    title="You know about these packages — no more reminders until the next delivery.">
              <Eye size={14} /> Acknowledge
            </Button>
          )}
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => run('check_now', p)}
                  title="Count the doorstep right now instead of waiting for the next trigger.">
            <RefreshCw size={14} className={busy ? 'animate-spin' : undefined} /> Check now
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

/** One line of the story, with the photographs if an alarm carried them. */
function EventRow({ ev, name, alert, onView }: {
  ev: DeliveryEvent
  name: string
  alert: InboxAlert | undefined
  onView: (a: InboxAlert) => void
}) {
  const ui = KIND_UI[ev.kind] ?? KIND_UI.cleared
  const isTaken = ev.kind === 'taken'
  const reasons = ev.who.reasons?.length ? ev.who.reasons.join(' · ') : null
  const pair = alert && ['before', 'after'].filter((n) => alert.images.includes(n))
  return (
    <li className="py-2 flex items-start gap-3 text-sm">
      <span className="mt-1.5 h-2.5 w-2.5 rounded-full shrink-0" style={{ background: ui.color }}
            role="img" aria-label={ui.label} />
      <span className="tabular-nums text-[var(--text-dim)] shrink-0 w-11 whitespace-nowrap">{hhmm(ev.time)}</span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className={`font-medium ${isTaken ? 'text-[var(--danger)]' : ''}`}>{ui.label}</span>
          <span className="text-[var(--text-dim)]">at</span>
          <span className="truncate max-w-[12rem]">{name}</span>
          {ev.kind !== 'reminder' && ev.kind !== 'cleared' && (
            <>
              <span className="text-[var(--text-dim)]">by</span>
              <span className={ev.who.kind === 'stranger' ? 'text-[var(--danger)]' : ''}>{whoLabel(ev.who)}</span>
            </>
          )}
          {ev.vehicle && (
            <span className="text-[var(--text-dim)] flex items-center gap-1"><VehicleIcon vehicle={ev.vehicle} /> {ev.vehicle}</span>
          )}
          {isTaken && ev.severity === 'high' && <Badge variant="destructive">high</Badge>}
          <span className="text-xs text-[var(--text-dim)] tabular-nums flex items-center gap-1"
                title="Packages counted before and after">
            {ev.count_before} <ArrowRight size={11} aria-label="to" /> {ev.count_after}
          </span>
          {ev.kind === 'picked_up' && ev.dwell_seconds != null && (
            <span className="text-xs text-[var(--text-dim)]">waited {span(ev.dwell_seconds)}</span>
          )}
          <Badge variant="neutral" className="ml-auto"
                 title={`Counted by ${METHOD_LABEL[ev.method] ?? ev.method}${ev.confidence != null ? ` · ${Math.round(ev.confidence * 100)}% confident` : ''}`}>
            {METHOD_LABEL[ev.method] ?? ev.method}
          </Badge>
        </div>
        {reasons && <div className="text-xs text-[var(--text-dim)] mt-0.5">{reasons}</div>}
      </div>
      {alert && pair && pair.length > 0 && (
        <button
          type="button"
          onClick={() => onView(alert)}
          aria-label={`Open the ${pair.length === 2 ? 'before and after photos' : 'photo'} for this event`}
          title="Open the photographs"
          className="flex items-center gap-1 shrink-0 rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
        >
          {pair.map((n, i) => (
            <span key={n} className="flex items-center gap-1">
              {i > 0 && <ArrowRight size={11} className="text-[var(--text-dim)]" aria-hidden />}
              <AuthedImage
                queryKey={['alert-image', alert.id, n]}
                fetchBlob={(signal) => alertsInboxService.getAlertImage(alert.id, n, signal)}
                alt={`${n} photo`}
                className="h-10 w-16 rounded object-cover border border-[var(--border)]"
              />
            </span>
          ))}
        </button>
      )}
    </li>
  )
}

/** The camera's current frame, refreshed every 30 s. The endpoint needs
 *  the JWT header, so it goes through the api client and an objectURL
 *  like every other snapshot on the site; the query key is shared with
 *  the App Catalog editors so the same still is not fetched twice. */
function Snapshot({ cameraId, alt, className }: { cameraId: number | null; alt: string; className?: string }) {
  const query = useQuery({
    queryKey: ['camera-snapshot', cameraId],
    queryFn: async () => (await apiService.getCameraSnapshot(cameraId!)).data as Blob,
    enabled: cameraId != null,
    retry: 0,
    staleTime: 30_000,
    refetchInterval: 30_000,
  })
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    const blob = query.data
    if (!(blob instanceof Blob)) { setUrl(null); return }
    const next = URL.createObjectURL(blob)
    setUrl(next)
    return () => URL.revokeObjectURL(next)
  }, [query.data])
  return (
    <div className={`flex items-center justify-center overflow-hidden bg-black ${className ?? ''}`}>
      {url
        ? <img src={url} alt={alt} className="h-full w-full object-cover" />
        : query.isError || cameraId == null
          ? <CameraOff size={20} className="text-[var(--text-dim)]" aria-label="No picture" />
          : <CameraIcon size={20} className="text-[var(--text-dim)] opacity-50 animate-pulse" aria-label="Loading picture" />}
    </div>
  )
}

function Stat({ icon, value, label, sub, tone, title }: {
  icon: React.ReactNode; value: React.ReactNode; label: string; sub?: string; tone?: 'warn' | 'danger'; title?: string
}) {
  const color = tone === 'danger' ? 'text-[var(--danger)]' : tone === 'warn' ? 'text-[var(--warn)]' : ''
  return (
    <Card>
      <CardContent className="flex items-center gap-3 py-3">
        <span className="text-[var(--text-dim)]" title={title}>{icon}</span>
        <div className="min-w-0">
          <div className={`text-xl font-semibold leading-none tabular-nums ${color}`}>{value}</div>
          <div className="text-xs text-[var(--text-dim)] mt-1 truncate" title={title}>{label}</div>
          {/* Its own line, so "since 14:02" survives a narrow tile — it is
              the one number a homeowner reads off this row. */}
          {sub && <div className="text-[11px] text-[var(--text-dim)] opacity-80 truncate">{sub}</div>}
        </div>
      </CardContent>
    </Card>
  )
}
