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

// Gates — the first-class page for the gate-controller app.
//
// Deliveries is a page you read; this is a page you press. A guard at a
// barrier has one question — "is it open, and if not, can I open it" —
// and one emergency: a car sitting at a gate that refused to move. So
// the hierarchy is state first (a pill you can read across a lobby),
// then the one big button, then everything else; and a faulted gate
// climbs to the top of the grid and carries its fault in the open.
//
// Two honesty rules the app's contract forces on us. A relay that does
// not report position gives us `monitored: false` — we say "not
// reported" rather than draw a confident "closed". And `dry_run` means
// the pulses go nowhere, which must be impossible to miss, because the
// whole page otherwise looks like it is working.

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, Ban, Camera as CameraIcon, CameraOff, CalendarClock, Cable, Clock, DoorOpen,
  FlaskConical, Hand, LockOpen, RefreshCw, Settings2, ShieldCheck, Timer, TriangleAlert, User,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { apiService } from '../lib/apiService'
import { useTranslation } from '../i18n'
import {
  Badge, Button, Card, CardContent, CardHeader, CardTitle,
  EmptyState, PageHeader, Skeleton, type BadgeVariant,
} from '../components/ui'
import type { RegisteredApp } from './AppCatalog'
import { AppPageHeader } from './apps/AppSetup'

export const GATES_CAPABILITY = 'gates'

export function findGatesApp(apps: RegisteredApp[] | undefined): RegisteredApp | null {
  return (
    (apps ?? []).find(
      (a) => a.enabled && ((a.manifest as any)?.provides ?? []).includes(GATES_CAPABILITY)
    ) ?? null
  )
}

/* ------------------------- The app's contract ------------------------- */

type GateState = 'closed' | 'open' | 'opening' | 'closing' | 'held' | 'faulted' | 'unknown'
type Transport = 'http' | 'gpio' | 'modbus' | 'onvif' | 'mqtt' | 'none'
type Decision = 'allow' | 'deny'
type GateAction =
  | 'opened' | 'refused' | 'held' | 'released' | 'fault' | 'test' | 'no_relay' | 'cooldown'

type GateEvent = {
  time: number
  gate?: string
  gate_name?: string | null
  plate: string | null
  owner: string | null
  unit?: string | null
  decision: Decision | null
  reason: string | null
  action: GateAction
  by: string | null
  note?: string | null
  confidence?: number | null
}

type GateRow = {
  id: string
  name: string
  state: GateState
  since: number | null
  transport: Transport
  address: string | null
  vendor: string | null
  monitored: boolean
  held_until: number | null
  hold_reason: string | null
  schedule_note: string | null
  dry_run: boolean
  opened_today: number
  faults_today: number
  last_event: GateEvent | null
}

type GatesState = {
  gates?: GateRow[]
  today?: { opened: number; denied: number; faults: number; manual: number }
  needs_wiring?: string[]
  events?: GateEvent[]
  dry_run?: boolean
  safety_note?: string | null
  since?: number | null
}

type Camera = { id: number; name: string }

/* ----------------------------- Helpers ------------------------------ */

function idFromHandle(handle: string): number | null {
  const m = /^cam-?(\d+)$/.exec(handle)
  return m ? Number(m[1]) : null
}

/** Unix seconds → "14:02", 24-hour, to match the app's schedule notes. */
function hhmm(ts: number | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** How the seven gate states look. `variant` carries the colour, but the
 *  label always carries the word too — a barrier board is read at a
 *  glance and often in daylight, and colour alone is not a state. */
const STATE_UI: Record<GateState, { key: string; hint: string; variant: BadgeVariant; busy?: boolean }> = {
  open:    { key: 'gates.state.open',    hint: 'gates.stateHint.open',    variant: 'success' },
  held:    { key: 'gates.state.held',    hint: 'gates.stateHint.held',    variant: 'success' },
  closed:  { key: 'gates.state.closed',  hint: 'gates.stateHint.closed',  variant: 'neutral' },
  opening: { key: 'gates.state.opening', hint: 'gates.stateHint.opening', variant: 'warning', busy: true },
  closing: { key: 'gates.state.closing', hint: 'gates.stateHint.closing', variant: 'warning', busy: true },
  faulted: { key: 'gates.state.faulted', hint: 'gates.stateHint.faulted', variant: 'destructive' },
  unknown: { key: 'gates.state.unknown', hint: 'gates.stateHint.unknown', variant: 'neutral' },
}

/** What happened, as a dot colour and a word. Refusals are deliberately
 *  dim, not red: a gate refusing an unregistered plate is the system
 *  working, and an operator who sees red for it stops reading the list. */
const ACTION_UI: Record<GateAction, { key: string; color: string }> = {
  opened:   { key: 'gates.event.opened',   color: 'var(--ok)' },
  refused:  { key: 'gates.event.refused',  color: 'var(--text-dim)' },
  held:     { key: 'gates.event.held',     color: 'var(--accent)' },
  released: { key: 'gates.event.released', color: 'var(--accent)' },
  fault:    { key: 'gates.event.fault',    color: 'var(--danger)' },
  test:     { key: 'gates.event.test',     color: 'var(--text-dim)' },
  no_relay: { key: 'gates.event.noRelay',  color: 'var(--warn)' },
  cooldown: { key: 'gates.event.cooldown', color: 'var(--text-dim)' },
}

const TRANSPORT_KEY: Record<Transport, string> = {
  http: 'gates.transport.http',
  gpio: 'gates.transport.gpio',
  modbus: 'gates.transport.modbus',
  onvif: 'gates.transport.onvif',
  mqtt: 'gates.transport.mqtt',
  none: 'gates.transport.none',
}

/** A faulted gate means a vehicle is very likely standing at a barrier
 *  that refused to move — it goes first, whatever the list order was. */
function byUrgency(a: GateRow, b: GateRow): number {
  const rank = (g: GateRow) => (g.state === 'faulted' ? 0 : g.faults_today > 0 ? 1 : 2)
  return rank(a) - rank(b) || a.name.localeCompare(b.name)
}

/* ------------------------------ Page ------------------------------ */

export function Gates() {
  const { t } = useTranslation()
  const qc = useQueryClient()

  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
  })
  const app = findGatesApp(appsQuery.data)

  const statusQuery = useQuery({
    queryKey: ['app-status', app?.id],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(app!.id)
      return data as { state?: GatesState }
    },
    enabled: Boolean(app),
    retry: 0,
    // A barrier takes several seconds to travel. Somebody who just
    // pressed Open watches this card until the pill changes, so the
    // poll has to be faster than the gate is.
    refetchInterval: 3000,
  })
  const state: GatesState = statusQuery.data?.state ?? {}

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

  // Keyed by camera, so pressing Open on one gate does not grey out the
  // buttons on the gate next to it.
  const act = useMutation({
    mutationFn: async ({ action, params }: { action: string; params: Record<string, unknown> }) =>
      apiService.invokeAppAction(app!.id, action, params),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['app-status', app?.id] }),
  })
  const run = (action: string, params: Record<string, unknown>) => act.mutate({ action, params })
  const busyCamera = act.isPending ? (act.variables?.params.camera as string | undefined) : undefined

  const gates = useMemo(() => [...(state.gates ?? [])].sort(byUrgency), [state.gates])
  const events = useMemo(
    () => [...(state.events ?? [])].sort((a, b) => b.time - a.time),
    [state.events]
  )
  const needsWiring = state.needs_wiring ?? []
  const today = state.today ?? { opened: 0, denied: 0, faults: 0, manual: 0 }
  const faulted = gates.filter((g) => g.state === 'faulted').length

  if (!appsQuery.isPending && !app) {
    return (
      <section className="space-y-4">
        <PageHeader title={t('gates.title')} description={t('gates.description')} />
        <EmptyState
          icon={<DoorOpen size={28} />}
          title={t('gates.noApp')}
          description={t('gates.noAppHelp')}
        />
      </section>
    )
  }

  return (
    <section className="space-y-4">
      <AppPageHeader
        app={app}
        title={t('gates.title')}
        description={t('gates.description')}
        actions={
          <>
            <Button size="sm" variant="outline" onClick={() => statusQuery.refetch()}>
              <RefreshCw size={14} /> {t('gates.refresh')}
            </Button>
            {app && (
              <Link to={`/app-catalog/${app.id}`}>
                <Button size="sm" variant="outline"><Settings2 size={14} /> {t('gates.configure')}</Button>
              </Link>
            )}
          </>
        }
      />

      {/* ── Commissioning mode ── The page looks alive in dry run; this
          banner is the only thing that says the barriers are not. */}
      {state.dry_run && (
        <div
          role="status"
          className="rounded border border-[var(--warn)]/50 bg-[var(--warn)]/10 px-3 py-2 text-sm flex items-start gap-2"
        >
          <FlaskConical size={15} className="text-[var(--warn)] shrink-0 mt-0.5" aria-hidden />
          <span>
            <b>{t('gates.dryRun.title')}</b>{' '}
            <span className="text-[var(--text-dim)]">{t('gates.dryRun.body')}</span>
          </span>
        </div>
      )}

      {/* ── Today ── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat icon={<LockOpen size={16} />} value={today.opened} label={t('gates.openedToday')} />
        <Stat icon={<Ban size={16} />} value={today.denied} label={t('gates.deniedToday')}
              title={t('gates.deniedTodayHint')} />
        <Stat icon={<TriangleAlert size={16} />} value={today.faults} label={t('gates.faultsToday')}
              tone={today.faults > 0 ? 'danger' : undefined} title={t('gates.faultsTodayHint')} />
        <Stat icon={<Hand size={16} />} value={today.manual} label={t('gates.manualToday')}
              title={t('gates.manualTodayHint')} />
      </div>

      {/* ── The one thing worth interrupting for ── */}
      {faulted > 0 && (
        <div
          role="alert"
          className="rounded border border-[var(--danger)]/50 bg-[var(--danger)]/10 px-3 py-2 text-sm flex items-start gap-2"
        >
          <TriangleAlert size={15} className="text-[var(--danger)] shrink-0 mt-0.5" aria-hidden />
          <span>
            <b>{faulted === 1 ? t('gates.faultBanner.titleOne') : t('gates.faultBanner.title', { count: faulted })}</b>{' '}
            <span className="text-[var(--text-dim)]">{t('gates.faultBanner.body')}</span>
          </span>
        </div>
      )}

      {/* ── The gates ── */}
      {statusQuery.isPending ? (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          <Skeleton className="h-72" /><Skeleton className="h-72" /><Skeleton className="h-72" />
        </div>
      ) : gates.length === 0 ? (
        <EmptyState
          icon={<Cable size={24} />}
          title={t('gates.empty.title')}
          description={t('gates.empty.body')}
          action={app ? (
            <Link to={`/app-catalog/${app.id}`}>
              <Button size="sm" variant="primary"><Settings2 size={14} /> {t('gates.configure')}</Button>
            </Link>
          ) : undefined}
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {gates.map((g) => (
            <GateCard key={g.id} gate={g} name={g.name || cameraName(g.id)}
                      dryRun={Boolean(g.dry_run || state.dry_run)}
                      busy={busyCamera === g.id} run={run} />
          ))}
        </div>
      )}

      {/* ── Decisions arriving with nowhere to go ── */}
      {needsWiring.length > 0 && (
        <Card>
          <CardHeader>
            <Cable size={16} className="text-[var(--text-dim)]" />
            <CardTitle>{t('gates.needsWiring.title')}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5">
            <p className="m-0 text-sm text-[var(--text-dim)]">{t('gates.needsWiring.body')}</p>
            <ul className="flex flex-wrap gap-1.5 list-none p-0 m-0">
              {needsWiring.map((c) => (
                <li key={c}>
                  <Badge variant="neutral" className="font-mono">{cameraName(c)}</Badge>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {/* ── Activity ── */}
      <Card>
        <CardHeader>
          <Clock size={16} className="text-[var(--text-dim)]" />
          <CardTitle>{t('gates.timeline.title')}</CardTitle>
          <span className="text-xs text-[var(--text-dim)]">{t('gates.timeline.sub')}</span>
          {statusQuery.isFetching && (
            <span className="ml-auto text-xs text-[var(--text-dim)]">{t('gates.updating')}</span>
          )}
        </CardHeader>
        <CardContent>
          {statusQuery.isPending ? (
            <Skeleton className="h-24" />
          ) : events.length === 0 ? (
            <div className="text-sm text-[var(--text-dim)] py-4 text-center">{t('gates.timeline.empty')}</div>
          ) : (
            <ol className="divide-y divide-[var(--border)] list-none p-0 m-0">
              {events.map((ev, i) => (
                <EventRow
                  key={`${ev.time}-${ev.gate ?? ''}-${i}`}
                  ev={ev}
                  name={ev.gate_name || (ev.gate ? cameraName(ev.gate) : '')}
                />
              ))}
            </ol>
          )}
        </CardContent>
      </Card>

      {/* ── Whose safety this is ── Quiet, but never absent: this page
          asks a barrier to move and cannot see what is under it. */}
      {state.safety_note && (
        <p className="m-0 flex items-start gap-2 px-1 text-xs text-[var(--text-dim)]">
          <ShieldCheck size={13} className="shrink-0 mt-0.5" aria-hidden />
          <span>{state.safety_note}</span>
        </p>
      )}
    </section>
  )
}

/* ----------------------------- Pieces ----------------------------- */

/** One gate: what it is doing, seen live, and the controls that make
 *  sense while it is doing it. */
function GateCard({ gate: g, name, dryRun, busy, run }: {
  gate: GateRow
  name: string
  /** This gate's own dry-run flag, or the whole app's — either way the
   *  pulse goes nowhere, and the card has to say so. */
  dryRun: boolean
  busy: boolean
  run: (action: string, params: Record<string, unknown>) => void
}) {
  const { t } = useTranslation()
  const camId = idFromHandle(g.id)
  const p = { camera: g.id }
  const isFaulted = g.state === 'faulted'
  const isHeld = g.state === 'held'
  const moving = g.state === 'opening' || g.state === 'closing'
  // An unmonitored relay reports nothing back, so whatever `state` says
  // is our last command echoed, not the barrier's position. Say that.
  const ui = STATE_UI[g.state] ?? STATE_UI.unknown
  const pill = g.monitored
    ? { label: t(ui.key), hint: t(ui.hint), variant: ui.variant }
    : { label: t('gates.state.notReported'), hint: t('gates.stateHint.notReported'), variant: 'neutral' as BadgeVariant }
  const meta = [
    t(TRANSPORT_KEY[g.transport] ?? 'gates.transport.none'),
    g.address,
    g.vendor,
  ].filter(Boolean) as string[]
  const last = g.last_event

  return (
    <Card className={isFaulted ? 'border-[var(--danger)]/60' : undefined}>
      <div className="relative">
        <Snapshot cameraId={camId} alt={t('gates.liveViewOf', { name })} className="aspect-video w-full rounded-t" />
        {dryRun && (
          <span className="absolute right-2 top-2 rounded-full bg-black/70 px-2 py-1 text-[11px] text-white flex items-center gap-1">
            <FlaskConical size={12} aria-hidden /> {t('gates.dryRunPill')}
          </span>
        )}
      </div>

      <CardHeader>
        <CardTitle className="truncate">{name}</CardTitle>
        <Badge variant={pill.variant} title={pill.hint}>
          {moving && g.monitored && (
            <span className="h-1.5 w-1.5 rounded-full bg-current animate-pulse" aria-hidden />
          )}
          {pill.label}
        </Badge>
      </CardHeader>

      <CardContent className="space-y-2">
        <div className="text-xs text-[var(--text-dim)]">
          {g.since ? t('gates.since', { time: hhmm(g.since) }) : t('gates.sinceUnknown')}
          {g.opened_today > 0 && (
            <> · {g.opened_today === 1
              ? t('gates.openedTodayCountOne')
              : t('gates.openedTodayCount', { count: g.opened_today })}</>
          )}
          {g.faults_today > 0 && (
            <> · <span className="text-[var(--danger)]">
              {g.faults_today === 1
                ? t('gates.faultsTodayCountOne')
                : t('gates.faultsTodayCount', { count: g.faults_today })}
            </span></>
          )}
        </div>

        {/* A fault is a car standing at a barrier. It gets its own block,
            above the controls, in the card's own colour. */}
        {isFaulted && (
          <div className="rounded border border-[var(--danger)]/50 bg-[var(--danger)]/10 px-2 py-1.5 text-xs flex items-start gap-1.5">
            <TriangleAlert size={13} className="text-[var(--danger)] shrink-0 mt-0.5" aria-hidden />
            <span>
              <b className="text-[var(--danger)]">{t('gates.fault.title')}</b>{' '}
              {/* `reason` on a fault event is why the plate was allowed
                  ("registered"), not why the gate refused to move —
                  printing it here would read as nonsense. The app's own
                  `note` is the only fault detail we can trust. */}
              <span className="text-[var(--text-dim)]">
                {last?.action === 'fault' && last.note ? last.note : t('gates.fault.body')}
              </span>
            </span>
          </div>
        )}

        {!g.monitored && (
          <div className="text-xs text-[var(--text-dim)] flex items-start gap-1.5">
            <Activity size={12} className="shrink-0 mt-0.5" aria-hidden />
            <span>{t('gates.notMonitored')}</span>
          </div>
        )}

        {isHeld && (
          <div className="text-xs flex items-start gap-1.5">
            <Timer size={12} className="shrink-0 mt-0.5 text-[var(--accent)]" aria-hidden />
            <span>
              {g.held_until ? t('gates.heldUntil', { time: hhmm(g.held_until) }) : t('gates.heldUntilReleased')}
              {g.hold_reason ? <span className="text-[var(--text-dim)]"> · {g.hold_reason}</span> : null}
            </span>
          </div>
        )}

        {g.schedule_note && (
          <div className="text-xs text-[var(--text-dim)] flex items-start gap-1.5">
            <CalendarClock size={12} className="shrink-0 mt-0.5" aria-hidden />
            <span>{g.schedule_note}</span>
          </div>
        )}

        <div className="text-[11px] text-[var(--text-dim)] font-mono break-words">
          {meta.join(' · ')}
        </div>

        {last ? (
          <div className="text-sm flex items-center gap-1.5 min-w-0">
            <span className="h-2 w-2 rounded-full shrink-0"
                  style={{ background: ACTION_UI[last.action]?.color ?? 'var(--text-dim)' }} aria-hidden />
            <span className="truncate">
              {t(ACTION_UI[last.action]?.key ?? 'gates.event.opened')} {hhmm(last.time)}
              {last.plate ? ` · ${last.plate}` : ''}
              {last.owner ? ` · ${last.owner}` : ''}
            </span>
          </div>
        ) : (
          <div className="text-sm text-[var(--text-dim)]">{t('gates.noEventsYet')}</div>
        )}

        {/* ── Controls ── Open is the button; everything else is quieter
            than it on purpose. */}
        <div className="space-y-1.5 pt-1">
          <Button
            size="sm"
            variant="primary"
            className="w-full justify-center"
            disabled={busy || moving}
            onClick={() => run('open_now', p)}
            aria-label={t('gates.action.openNowLong', { name })}
            title={isFaulted ? t('gates.action.openNowFaultedHint') : t('gates.action.openNowHint')}
          >
            <LockOpen size={14} /> {t('gates.action.openNow')}
          </Button>

          {isHeld ? (
            <Button size="sm" variant="outline" className="w-full justify-center" disabled={busy}
                    onClick={() => run('release_hold', p)}
                    aria-label={t('gates.action.releaseLong', { name })}
                    title={t('gates.action.releaseHint')}>
              <Timer size={14} /> {t('gates.action.release')}
            </Button>
          ) : (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-[var(--text-dim)]" id={`hold-${g.id}`}>{t('gates.action.hold')}</span>
              <Button size="sm" variant="outline" disabled={busy}
                      aria-describedby={`hold-${g.id}`}
                      aria-label={t('gates.action.hold15Long', { name })}
                      onClick={() => run('hold_open', { ...p, minutes: 15 })}>
                {t('gates.action.hold15')}
              </Button>
              <Button size="sm" variant="outline" disabled={busy}
                      aria-describedby={`hold-${g.id}`}
                      aria-label={t('gates.action.hold60Long', { name })}
                      onClick={() => run('hold_open', { ...p, minutes: 60 })}>
                {t('gates.action.hold60')}
              </Button>
              <Button size="sm" variant="outline" disabled={busy}
                      aria-describedby={`hold-${g.id}`}
                      aria-label={t('gates.action.holdOpenEndedLong', { name })}
                      title={t('gates.action.holdOpenEndedHint')}
                      onClick={() => run('hold_open', { ...p, minutes: 0 })}>
                {t('gates.action.holdOpenEnded')}
              </Button>
            </div>
          )}

          <Button size="sm" variant="ghost" disabled={busy} onClick={() => run('test', p)}
                  aria-label={t('gates.action.testLong', { name })} title={t('gates.action.testHint')}>
            <FlaskConical size={14} /> {t('gates.action.test')}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

/** One decision, with what the gate did about it. Read as a sentence by
 *  a screen reader: time, gate, plate, allowed or denied and why, what
 *  happened, and who did it if a person did. */
function EventRow({ ev, name }: { ev: GateEvent; name: string }) {
  const { t } = useTranslation()
  const ui = ACTION_UI[ev.action] ?? ACTION_UI.opened
  const denied = ev.decision === 'deny'
  const isFault = ev.action === 'fault'
  const who = ev.by ? t('gates.byOperator', { name: ev.by }) : t('gates.automatic')
  return (
    <li className="py-2 flex items-start gap-3 text-sm">
      <span className="mt-1.5 h-2.5 w-2.5 rounded-full shrink-0" style={{ background: ui.color }}
            role="img" aria-label={t(ui.key)} />
      <span className="tabular-nums text-[var(--text-dim)] shrink-0 w-11 whitespace-nowrap">{hhmm(ev.time)}</span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className={`font-medium ${isFault ? 'text-[var(--danger)]' : ''}`}>{t(ui.key)}</span>
          {name && (
            <>
              <span className="text-[var(--text-dim)]">{t('gates.at')}</span>
              <span className="truncate max-w-[12rem]">{name}</span>
            </>
          )}
          {ev.plate && <span className="font-mono tracking-wide">{ev.plate}</span>}
          {(ev.owner || ev.unit) && (
            <span className="text-[var(--text-dim)] truncate max-w-[12rem]">
              {ev.owner ?? t('gates.unit', { unit: ev.unit ?? '' })}
            </span>
          )}
          {ev.decision && (
            // A refusal is the system working. Neutral, not destructive.
            <Badge variant={denied ? 'neutral' : 'success'}>
              {denied ? t('gates.decision.deny') : t('gates.decision.allow')}
            </Badge>
          )}
          {ev.reason && <span className="text-xs text-[var(--text-dim)]">{ev.reason}</span>}
          {ev.confidence != null && (
            <span className="text-xs text-[var(--text-dim)] tabular-nums"
                  title={t('gates.confidenceHint')}>
              {t('gates.confidence', { pct: Math.round(ev.confidence * 100) })}
            </span>
          )}
          <span className="text-xs text-[var(--text-dim)] ml-auto flex items-center gap-1">
            {ev.by && <User size={11} aria-hidden />}{who}
          </span>
        </div>
        {ev.note && <div className="text-xs text-[var(--text-dim)] mt-0.5">{ev.note}</div>}
      </div>
    </li>
  )
}

/** The camera's current frame, refreshed every 30 s. The endpoint needs
 *  the JWT header, so it goes through the api client and an objectURL
 *  like every other snapshot on the site; the query key is shared with
 *  the other pages so the same still is not fetched twice. */
function Snapshot({ cameraId, alt, className }: { cameraId: number | null; alt: string; className?: string }) {
  const { t } = useTranslation()
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
          ? <CameraOff size={20} className="text-[var(--text-dim)]" aria-label={t('gates.noPicture')} />
          : <CameraIcon size={20} className="text-[var(--text-dim)] opacity-50 animate-pulse" aria-label={t('gates.loadingPicture')} />}
    </div>
  )
}

function Stat({ icon, value, label, tone, title }: {
  icon: React.ReactNode; value: React.ReactNode; label: string; tone?: 'warn' | 'danger'; title?: string
}) {
  const color = tone === 'danger' ? 'text-[var(--danger)]' : tone === 'warn' ? 'text-[var(--warn)]' : ''
  return (
    <Card>
      <CardContent className="flex items-center gap-3 py-3">
        <span className="text-[var(--text-dim)]" title={title}>{icon}</span>
        <div className="min-w-0">
          <div className={`text-xl font-semibold leading-none tabular-nums ${color}`}>{value}</div>
          <div className="text-xs text-[var(--text-dim)] mt-1 truncate" title={title}>{label}</div>
        </div>
      </CardContent>
    </Card>
  )
}
