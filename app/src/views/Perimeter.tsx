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

// Perimeter — the first-class page for the intrusion-detection app, laid
// out the way an intrusion panel is: the site's arming state first and
// biggest, the countdown next to it, then each camera as a zone tile with
// its own state and its own arm / disarm / bypass, then who is inside
// right now, then the alarms with their snapshots.
//
// The page is a panel keypad, not a report: every state it shows has the
// control that changes it within reach, because the minute an alarm goes
// off is not the minute to go hunting through a config editor. The app's
// live state is read from its /state; arm, disarm, bypass, acknowledge
// and clear_override are its actions.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, BellOff, BellRing, CheckCircle2, Clock, EyeOff, Lock, PenLine,
  RefreshCw, Shield, ShieldAlert, ShieldCheck, ShieldOff, Timer, Unlock, Users,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { apiService } from '../lib/apiService'
import { useTranslation } from '../i18n'
import {
  Badge, Button, Card, CardContent, CardHeader, CardTitle,
  EmptyState, PageHeader, Skeleton,
} from '../components/ui'
import { AlarmsTable } from '../components/alarms/AlarmsTable'
import { useAckAlarms, useAlarmsList } from '../components/alarms/useAlarmsList'
import type { RegisteredApp } from './AppCatalog'

export const INTRUSION_CAPABILITY = 'intrusion'
const SOURCE = 'intrusion-detection'

export function findIntrusionApp(apps: RegisteredApp[] | undefined): RegisteredApp | null {
  return (
    (apps ?? []).find(
      (a) => a.enabled && ((a.manifest as any)?.provides ?? []).includes(INTRUSION_CAPABILITY)
    ) ?? null
  )
}

/** The panel states, in the app's vocabulary. */
type CamState = 'disarmed' | 'arming' | 'armed' | 'breach' | 'alarm' | 'bypassed' | string

type CameraRow = {
  camera: string
  zone: string
  drawn: boolean
  state: CamState
  countdown_s?: number | null
  override?: boolean | null
  intruders: number
  breaches_today: number
  alarms_today: number
  last?: number | null
}
type Intruder = {
  camera: string
  label: string
  track: string
  inside_s: number
  stage: 'watching' | 'counted' | 'breach' | 'alarm' | string
}
type Recent = { time: number; message?: string; level?: string; camera?: string }
type IntrusionState = {
  armed_count?: number
  camera_count?: number
  in_alarm?: number
  bypassed?: number
  today?: { breaches: number; alarms: number; since?: string }
  arm_mode?: 'schedule' | 'always' | 'manual' | 'off' | string
  armed_hours?: { start: string; end: string } | null
  entry_delay_seconds?: number
  exit_delay_seconds?: number
  min_presence_seconds?: number
  escalate_after_seconds?: number
  overridden?: boolean
  per_camera?: CameraRow[]
  intruders?: Intruder[]
  needs_zone?: string[]
  recent?: Recent[]
}
type Camera = { id: number; name: string }

function idFromHandle(handle: string): number | null {
  const m = /^cam-?(\d+)$/.exec(handle)
  return m ? Number(m[1]) : null
}

function ago(ts: number | null | undefined): string {
  if (!ts) return '—'
  const age = Math.max(0, Date.now() / 1000 - ts)
  if (age < 60) return 'just now'
  if (age < 3600) return `${Math.floor(age / 60)}m ago`
  if (age < 86400) return `${Math.floor(age / 3600)}h ago`
  return `${Math.floor(age / 86400)}d ago`
}

/** 75 → "1:15", 12 → "12s". */
function mmss(s: number | null | undefined): string {
  const v = Math.max(0, Math.floor(s ?? 0))
  if (v < 60) return `${v}s`
  return `${Math.floor(v / 60)}:${String(v % 60).padStart(2, '0')}`
}

// One table for every state the page renders, so the tile, the banner and
// the badge can never disagree about what "breach" looks like.
const STATE_UI: Record<string, {
  label: string
  variant: 'destructive' | 'critical' | 'warning' | 'neutral' | 'success'
  color: string
  icon: React.ReactNode
  hint: string
}> = {
  armed: {
    label: 'Armed', variant: 'success', color: 'var(--ok)', icon: <ShieldCheck size={16} />,
    hint: 'Watching the zone. A watched class inside it raises an alarm.',
  },
  arming: {
    label: 'Arming', variant: 'warning', color: 'var(--warn)', icon: <Timer size={16} />,
    hint: 'Exit delay running — the zone goes live when it reaches zero.',
  },
  breach: {
    label: 'Breach', variant: 'warning', color: 'var(--warn)', icon: <ShieldAlert size={16} />,
    hint: 'Someone is in the zone. Entry delay running — disarm now to stop the alarm.',
  },
  alarm: {
    label: 'Alarm', variant: 'critical', color: 'var(--danger)', icon: <BellRing size={16} />,
    hint: 'Alarm raised, with a snapshot. Acknowledge to re-arm without waiting for the reset.',
  },
  disarmed: {
    label: 'Disarmed', variant: 'neutral', color: 'var(--text-dim)', icon: <ShieldOff size={16} />,
    hint: 'Not watching. Objects in the zone raise nothing.',
  },
  bypassed: {
    label: 'Bypassed', variant: 'neutral', color: 'var(--text-dim)', icon: <EyeOff size={16} />,
    hint: 'Deliberately excluded for a while — it comes back on its own.',
  },
}
const stateUi = (s: CamState) => STATE_UI[s] ?? STATE_UI.disarmed

const STAGE_TONE: Record<string, { variant: 'destructive' | 'critical' | 'warning' | 'neutral'; text: string }> = {
  watching: { variant: 'neutral', text: 'watching' },
  counted: { variant: 'warning', text: 'breach' },
  breach: { variant: 'warning', text: 'breach' },
  alarm: { variant: 'critical', text: 'alarm' },
}

const BYPASS_CHOICES = [15, 30, 60, 240] as const

/* ----------------------------- Page ----------------------------- */

export function Perimeter() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [bypassFor, setBypassFor] = useState<string | null>(null)

  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
  })
  const app = findIntrusionApp(appsQuery.data)

  const statusQuery = useQuery({
    queryKey: ['app-status', app?.id],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(app!.id)
      return data as { state?: IntrusionState }
    },
    enabled: Boolean(app),
    retry: 0,
    // A countdown is on screen; a panel that lags is a panel nobody trusts.
    refetchInterval: 2000,
  })
  const state: IntrusionState = statusQuery.data?.state ?? {}

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

  const act = useMutation({
    mutationFn: async ({ action, params }: { action: string; params?: Record<string, unknown> }) =>
      apiService.invokeAppAction(app!.id, action, params ?? {}),
    onSuccess: () => {
      setBypassFor(null)
      qc.invalidateQueries({ queryKey: ['app-status', app?.id] })
    },
  })
  const run = (action: string, params?: Record<string, unknown>) => act.mutate({ action, params })

  const rows = state.per_camera ?? []
  const intruders = state.intruders ?? []
  const recent = [...(state.recent ?? [])].reverse().slice(0, 12)
  const needsZone = state.needs_zone ?? []

  const alarms = useAlarmsList({
    queryKeyPrefix: 'intrusion-alarms', sourceName: SOURCE,
    unacked: false, severity: null, page: 1, pageSize: 8, skip: 0,
  })
  const ack = useAckAlarms()
  const alarmRows = alarms.rows.filter((a) => a.source_name === SOURCE)

  if (!appsQuery.isPending && !app) {
    return (
      <section className="space-y-4">
        <PageHeader title={t('perimeter.title')} description={t('perimeter.description')} />
        <EmptyState
          icon={<Shield size={28} />}
          title={t('perimeter.noApp')}
          description="Install and enable Intrusion Detection from the App Catalog. It rides the detection stream the platform already produces — no extra model, no GPU — arms the zones you draw on a schedule or on command, and raises one alarm per intruder with a snapshot."
        />
      </section>
    )
  }

  const today = state.today ?? { breaches: 0, alarms: 0 }
  const inAlarm = state.in_alarm ?? 0
  const armed = state.armed_count ?? 0
  const total = state.camera_count ?? rows.length
  const alarmRow = rows.find((r) => r.state === 'alarm')
  const breachRow = rows.find((r) => r.state === 'breach')
  const site: CamState = alarmRow ? 'alarm' : breachRow ? 'breach'
    : armed > 0 ? 'armed' : rows.some((r) => r.state === 'arming') ? 'arming' : 'disarmed'
  const ui = stateUi(site)
  // The one countdown worth the headline: the entry delay if something is
  // in the zone now, otherwise an exit delay still running somewhere.
  const countdownRow = breachRow ?? rows.find((r) => r.state === 'arming')
  const countdown = countdownRow?.countdown_s ?? null

  const hours = state.armed_hours
  const policy =
    state.arm_mode === 'schedule'
      ? hours ? `armed ${hours.start}–${hours.end}` : 'armed around the clock'
      : state.arm_mode === 'always' ? 'armed around the clock'
        : state.arm_mode === 'manual' ? 'manual arming only'
          : 'watching only — never alarms'

  return (
    <section className="space-y-4">
      <PageHeader
        title={t('perimeter.title')}
        description={t('perimeter.description')}
        actions={
          <>
            <Button size="sm" variant="outline" onClick={() => statusQuery.refetch()}>
              <RefreshCw size={14} /> Refresh
            </Button>
            {app && (
              <Link to={`/app-catalog/${app.id}`}>
                <Button size="sm" variant="outline"><PenLine size={14} /> Draw zones</Button>
              </Link>
            )}
          </>
        }
      />

      {/* ── The panel: site state, its countdown, and the controls that
             change it. Everything else on the page is detail. ── */}
      <Card
        className={`border-l-4 ${site === 'alarm' ? 'bg-[var(--danger)]/5' : ''}`}
        style={{ borderLeftColor: ui.color }}
      >
        <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-3 py-4">
          <div className="flex items-center gap-3 min-w-[13rem]">
            <span style={{ color: ui.color }} className={site === 'alarm' ? 'animate-pulse' : ''}>
              {site === 'alarm' ? <BellRing size={30} /> : site === 'armed' ? <ShieldCheck size={30} />
                : site === 'breach' ? <ShieldAlert size={30} /> : site === 'arming' ? <Timer size={30} />
                  : <ShieldOff size={30} />}
            </span>
            <div>
              <div className="text-2xl font-semibold leading-none" style={{ color: ui.color }}>{ui.label}</div>
              <div className="text-xs text-[var(--text-dim)] mt-1">
                {armed} of {total} cameras armed · {policy}
              </div>
            </div>
          </div>

          {countdown != null && countdown > 0 && (
            <div className="flex items-center gap-2" title={ui.hint}>
              <Clock size={16} className="text-[var(--warn)]" />
              <div>
                <div className="text-xl font-semibold tabular-nums leading-none text-[var(--warn)]">{mmss(countdown)}</div>
                <div className="text-[11px] text-[var(--text-dim)] mt-1">
                  {countdownRow?.state === 'breach'
                    ? `until the alarm · ${cameraName(countdownRow.camera)}`
                    : `until ${cameraName(countdownRow!.camera)} goes live`}
                </div>
              </div>
            </div>
          )}

          <div className="text-sm text-[var(--text-dim)] max-w-sm hidden lg:block">{ui.hint}</div>

          <div className="flex flex-wrap items-center gap-2 ml-auto">
            {inAlarm > 0 && (
              <Button variant="danger" disabled={act.isPending} onClick={() => run('acknowledge')}>
                <CheckCircle2 size={14} /> Acknowledge
              </Button>
            )}
            <Button variant={armed > 0 ? 'outline' : 'primary'} disabled={act.isPending} onClick={() => run('arm')}>
              <Lock size={14} /> Arm all
            </Button>
            <Button variant="outline" disabled={act.isPending} onClick={() => run('disarm')}>
              <Unlock size={14} /> Disarm all
            </Button>
          </div>
        </CardContent>
      </Card>

      {state.overridden && (
        <div className="rounded border border-[var(--warn)]/40 bg-[var(--warn)]/5 px-3 py-2 text-sm flex flex-wrap items-center gap-2">
          <AlertTriangle size={14} className="text-[var(--warn)]" />
          <span>
            A manual arm or disarm is in force and is overriding {policy}. It expires on its own, so the site cannot be left open by accident.
          </span>
          <Button size="sm" variant="outline" className="ml-auto" disabled={act.isPending} onClick={() => run('clear_override')}>
            Back to schedule
          </Button>
        </div>
      )}

      {/* ── Headline ── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat icon={<ShieldCheck size={16} />} value={`${armed}/${total}`} label="cameras armed" />
        <Stat icon={<BellRing size={16} />} value={inAlarm} label="in breach or alarm" tone={inAlarm > 0 ? 'danger' : undefined} />
        <Stat icon={<ShieldAlert size={16} />} value={today.breaches} label="breaches today" />
        <Stat icon={<AlertTriangle size={16} />} value={today.alarms} label="alarms today" tone={today.alarms > 0 ? 'warn' : undefined} />
      </div>

      {needsZone.length > 0 && app && (
        <div className="rounded border border-[var(--warn)]/40 bg-[var(--warn)]/5 px-3 py-2 text-sm flex flex-wrap items-center gap-2">
          <PenLine size={14} className="text-[var(--warn)]" />
          <span>
            {needsZone.length === 1 ? 'One camera has no zone drawn:' : `${needsZone.length} cameras have no zone drawn:`}{' '}
            <b>{needsZone.map(cameraName).join(', ')}</b>. The whole frame is armed there — on a perimeter camera that usually alarms on the road behind the fence too.
          </span>
          <Link to={`/app-catalog/${app.id}`} className="ml-auto text-[var(--accent)] underline">Draw it</Link>
        </div>
      )}

      {/* ── Zones ── */}
      {statusQuery.isPending ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-3">
          <Skeleton className="h-40" /><Skeleton className="h-40" /><Skeleton className="h-40" />
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<Shield size={24} />}
          title="No cameras selected"
          description="Select cameras for Intrusion Detection and draw each one's zone (App Catalog → Intrusion Detection → Configure). Arming applies within a few seconds."
        />
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-3">
          {rows.map((r) => {
            const s = stateUi(r.state)
            const live = r.state === 'alarm' || r.state === 'breach'
            return (
              <Card key={r.camera} className={live ? 'border-[var(--danger)]/50' : undefined}>
                <CardHeader>
                  <span style={{ color: s.color }}>{s.icon}</span>
                  <CardTitle>{cameraName(r.camera)}</CardTitle>
                  <Badge variant={s.variant} title={s.hint}>
                    {s.label}{r.countdown_s ? ` · ${mmss(r.countdown_s)}` : ''}
                  </Badge>
                  {r.override != null && (
                    <Badge variant="info" title="A manual arm or disarm is holding this camera; it expires on its own.">
                      manual
                    </Badge>
                  )}
                  <span className="ml-auto text-xs text-[var(--text-dim)]">{ago(r.last)}</span>
                </CardHeader>
                <CardContent>
                  <div className="text-xs text-[var(--text-dim)] mb-2 truncate" title={r.zone}>
                    {r.drawn ? r.zone : 'no zone drawn — whole frame'}
                  </div>
                  <div className="flex flex-wrap gap-5 mb-3">
                    <Mini value={r.intruders} label="inside now" tone={r.intruders > 0 ? 'warn' : undefined} />
                    <Mini value={r.breaches_today} label="breaches today" />
                    <Mini value={r.alarms_today} label="alarms today" tone={r.alarms_today > 0 ? 'warn' : undefined} />
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {r.state === 'alarm' ? (
                      <Button size="sm" variant="danger" disabled={act.isPending}
                              onClick={() => run('acknowledge', { camera: r.camera })}>
                        <CheckCircle2 size={14} /> Acknowledge
                      </Button>
                    ) : r.state === 'disarmed' || r.state === 'bypassed' ? (
                      <Button size="sm" variant="primary" disabled={act.isPending}
                              onClick={() => run(r.state === 'bypassed' ? 'bypass' : 'arm',
                                                 r.state === 'bypassed' ? { camera: r.camera, minutes: 0 } : { camera: r.camera })}>
                        <Lock size={14} /> {r.state === 'bypassed' ? 'End bypass' : 'Arm'}
                      </Button>
                    ) : (
                      <Button size="sm" variant="outline" disabled={act.isPending}
                              onClick={() => run('disarm', { camera: r.camera })}>
                        <Unlock size={14} /> Disarm
                      </Button>
                    )}

                    {r.state !== 'bypassed' && (
                      bypassFor === r.camera ? (
                        <span className="flex items-center gap-1">
                          {BYPASS_CHOICES.map((m) => (
                            <Button key={m} size="sm" variant="outline" disabled={act.isPending}
                                    onClick={() => run('bypass', { camera: r.camera, minutes: m })}>
                              {m < 60 ? `${m}m` : `${m / 60}h`}
                            </Button>
                          ))}
                          <Button size="sm" variant="ghost" onClick={() => setBypassFor(null)}>Cancel</Button>
                        </span>
                      ) : (
                        <Button size="sm" variant="ghost" onClick={() => setBypassFor(r.camera)}
                                title="Exclude this camera for a while — contractors in the yard, a bay open for the afternoon.">
                          <BellOff size={14} /> Bypass
                        </Button>
                      )
                    )}
                  </div>
                </CardContent>
              </Card>
            )
          })}
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-5 gap-4">
        {/* ── Inside now + recent ── */}
        <div className="xl:col-span-2 space-y-4">
          <Card>
            <CardHeader>
              <Users size={16} className="text-[var(--text-dim)]" />
              <CardTitle>Inside now</CardTitle>
              {statusQuery.isFetching && <span className="ml-auto text-xs text-[var(--text-dim)]">updating…</span>}
            </CardHeader>
            <CardContent>
              {intruders.length === 0 ? (
                <div className="text-sm text-[var(--text-dim)] py-3 text-center">Nothing inside a zone.</div>
              ) : (
                <ul className="divide-y divide-[var(--border)] text-sm">
                  {intruders.map((i) => {
                    const tone = STAGE_TONE[i.stage] ?? STAGE_TONE.watching
                    return (
                      <li key={`${i.camera}/${i.track}`} className="flex items-center gap-2 py-2">
                        <span className="font-medium truncate max-w-[9rem]">{cameraName(i.camera)}</span>
                        <span className="text-[var(--text-dim)]">{i.label}</span>
                        <Badge variant={tone.variant}>{tone.text}</Badge>
                        <span className="ml-auto tabular-nums font-semibold">{mmss(i.inside_s)}</span>
                      </li>
                    )
                  })}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader><CardTitle>Recent</CardTitle></CardHeader>
            <CardContent>
              {recent.length === 0 ? (
                <div className="text-sm text-[var(--text-dim)] py-3 text-center">Nothing yet.</div>
              ) : (
                <ul className="divide-y divide-[var(--border)] text-sm">
                  {recent.map((c, i) => (
                    <li key={i} className="flex items-center gap-2 py-1.5">
                      {c.level && c.level !== 'info'
                        ? <AlertTriangle size={14} className="text-[var(--danger)] shrink-0" />
                        : <Shield size={14} className="text-[var(--text-dim)] shrink-0" />}
                      <span className="truncate">
                        {c.message && c.camera ? c.message.replace(c.camera, cameraName(c.camera)) : (c.message ?? '')}
                      </span>
                      <span className="ml-auto text-xs text-[var(--text-dim)] shrink-0">{ago(c.time)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </div>

        {/* ── Alarms with evidence ── */}
        <div className="xl:col-span-3">
          <AlarmsTable
            caption="Intrusion alarms"
            rows={alarmRows}
            cameraLabel={(handle) => (handle ? cameraName(handle) : '—')}
            onAck={(ids) => ack.mutate(ids)}
            ackPending={ack.isPending}
            isPending={alarms.isPending}
            isFetching={alarms.isFetching}
            isError={alarms.isError}
            error={alarms.error}
            emptyTitle="No intrusion alarms"
            emptyDescription="Alarms appear here with a snapshot from the camera as they fire. If nothing ever fires, check that the cameras are armed and that a zone is drawn where people actually walk."
            fillHeight={false}
            footer={
              <div className="text-xs text-[var(--text-dim)] px-1 py-1">
                Entry delay {mmss(state.entry_delay_seconds)} · exit delay {mmss(state.exit_delay_seconds)} ·
                {' '}presence {mmss(state.min_presence_seconds)}
                {state.escalate_after_seconds ? ` · escalates after ${mmss(state.escalate_after_seconds)}` : ''}.
                {' '}<Link to="/alarms" className="text-[var(--accent)] underline">All alarms</Link>
              </div>
            }
          />
        </div>
      </div>
    </section>
  )
}

/* --------------------------- Pieces ----------------------------- */

function Stat({ icon, value, label, tone }: {
  icon: React.ReactNode; value: React.ReactNode; label: string; tone?: 'warn' | 'danger'
}) {
  const color = tone === 'danger' ? 'text-[var(--danger)]' : tone === 'warn' ? 'text-[var(--warn)]' : ''
  return (
    <Card>
      <CardContent className="flex items-center gap-3 py-3">
        <span className="text-[var(--text-dim)]">{icon}</span>
        <div className="min-w-0">
          <div className={`text-xl font-semibold leading-none ${color}`}>{value}</div>
          <div className="text-xs text-[var(--text-dim)] mt-1 truncate">{label}</div>
        </div>
      </CardContent>
    </Card>
  )
}

function Mini({ value, label, tone }: { value: React.ReactNode; label: string; tone?: 'warn' }) {
  return (
    <div>
      <div className={`text-lg font-semibold leading-none ${tone === 'warn' ? 'text-[var(--warn)]' : ''}`}>{value}</div>
      <div className="text-[11px] text-[var(--text-dim)] mt-0.5">{label}</div>
    </div>
  )
}
