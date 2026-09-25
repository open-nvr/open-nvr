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

// Left Items — the first-class page for the abandoned-object app.
//
// An unattended-baggage alarm tells an operator a bag exists. What they
// actually decide with is: how long has it been alone, is anyone with
// it, who put it down, and is it still there. So this page is a triage
// queue of items rather than a list of alerts — each row carries its
// own countdown to the threshold, who left it, and the three things an
// operator can do about it (acknowledge, resolve, or say "that is a bin,
// stop telling me"). Items someone is standing with sit in a quieter
// list below, because they are the normal case, not an incident.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Armchair, Briefcase, CheckCircle2, Eye, Moon, PackageX,
  PenLine, RefreshCw, UserCheck, UserX,
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
import { AppCamerasCard } from './apps/AppCamerasCard'
import { AppPageHeader, AppConfigureButton } from './apps/AppSetup'

export const LEFT_ITEMS_CAPABILITY = 'left_items'
const SOURCE = 'abandoned-object'

export function findLeftItemsApp(apps: RegisteredApp[] | undefined): RegisteredApp | null {
  return (
    (apps ?? []).find(
      (a) => a.enabled && ((a.manifest as any)?.provides ?? []).includes(LEFT_ITEMS_CAPABILITY)
    ) ?? null
  )
}

/** The item lifecycle, in the app's vocabulary. */
type ItemState = 'moving' | 'with-owner' | 'unattended' | 'abandoned' | 'escalated' | string

type Item = {
  camera: string
  track: string
  label: string
  state: ItemState
  alone_s: number
  settled_s: number
  owner?: string | null
  owner_left_s?: number | null
  acked?: boolean
  progress: number
}
type CameraRow = {
  camera: string
  zone: string
  drawn: boolean
  items: number
  unattended: number
  items_today: number
  alerts_today: number
  reclaimed_today: number
  fixtures: number
  last?: number | null
}
type Recent = { time: number; message?: string; level?: string; camera?: string }
type LeftItemsState = {
  unattended_now?: number
  abandoned_now?: number
  attended_now?: number
  camera_count?: number
  longest_alone_s?: number
  today?: { items: number; alerts: number; reclaimed: number; since?: string }
  unattended_seconds?: number
  settle_seconds?: number
  owner_grace_seconds?: number
  escalate_after_seconds?: number
  alerts_active_now?: boolean
  active_hours?: { start: string; end: string } | null
  fixtures?: number
  per_camera?: CameraRow[]
  items?: Item[]
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

// One table for every state the page renders, so the row, the badge and
// the bar can never disagree about what "unattended" looks like.
const STATE_UI: Record<string, {
  label: string
  variant: 'destructive' | 'critical' | 'warning' | 'neutral' | 'success'
  color: string
  hint: string
}> = {
  'with-owner': {
    label: 'With owner', variant: 'success', color: 'var(--ok)',
    hint: 'Somebody is standing with it. Normal, and not counting down.',
  },
  moving: {
    label: 'Moving', variant: 'neutral', color: 'var(--text-dim)',
    hint: 'Still being carried — it has not settled anywhere yet.',
  },
  unattended: {
    label: 'Unattended', variant: 'warning', color: 'var(--warn)',
    hint: 'Alone. The bar shows how close it is to the threshold.',
  },
  abandoned: {
    label: 'Abandoned', variant: 'destructive', color: 'var(--danger)',
    hint: 'Past the threshold and alerted, with a snapshot.',
  },
  escalated: {
    label: 'Escalated', variant: 'critical', color: 'var(--danger)',
    hint: 'Still there and unacknowledged after the escalation delay.',
  },
}
const stateUi = (s: ItemState) => STATE_UI[s] ?? STATE_UI.moving
const isLive = (s: ItemState) => s === 'unattended' || s === 'abandoned' || s === 'escalated'

/* ----------------------------- Page ----------------------------- */

export function LeftItems() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [confirmFixture, setConfirmFixture] = useState<string | null>(null)

  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
  })
  const app = findLeftItemsApp(appsQuery.data)

  const statusQuery = useQuery({
    queryKey: ['app-status', app?.id],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(app!.id)
      return data as { state?: LeftItemsState }
    },
    enabled: Boolean(app),
    retry: 0,
    // Countdowns are on screen; a page that lags is a page nobody trusts.
    refetchInterval: 2000,
  })
  const state: LeftItemsState = statusQuery.data?.state ?? {}

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
      setConfirmFixture(null)
      qc.invalidateQueries({ queryKey: ['app-status', app?.id] })
    },
  })
  const run = (action: string, params?: Record<string, unknown>) => act.mutate({ action, params })

  const items = state.items ?? []
  const live = items.filter((i) => isLive(i.state))
  const settled = items.filter((i) => !isLive(i.state))
  const recent = [...(state.recent ?? [])].reverse().slice(0, 12)
  const needsZone = state.needs_zone ?? []
  const rows = state.per_camera ?? []
  const threshold = state.unattended_seconds ?? 60

  const alarms = useAlarmsList({
    queryKeyPrefix: 'left-items-alarms', sourceName: SOURCE,
    unacked: false, severity: null, page: 1, pageSize: 8, skip: 0,
  })
  const ack = useAckAlarms()
  const alarmRows = alarms.rows.filter((a) => a.source_name === SOURCE)

  if (!appsQuery.isPending && !app) {
    return (
      <section className="space-y-4">
        <PageHeader title={t('leftItems.title')} description={t('leftItems.description')} />
        <EmptyState
          icon={<Briefcase size={28} />}
          title={t('leftItems.noApp')}
          description="Install and enable Abandoned Object from the App Catalog. It rides the detection stream the platform already produces — no extra model, no GPU — and follows every item left in a zone you draw: who was with it, how long it has been alone, and whether anyone came back for it."
        />
      <AppCamerasCard app={app} />
      </section>
    )
  }

  const today = state.today ?? { items: 0, alerts: 0, reclaimed: 0 }
  const quiet = state.alerts_active_now === false
  const hours = state.active_hours
  const policy = quiet
    ? `outside alert hours — quiet until ${hours?.start ?? '—'}`
    : `threshold ${mmss(threshold)} alone${state.escalate_after_seconds ? ` · escalate +${mmss(state.escalate_after_seconds)}` : ''}`

  return (
    <section className="space-y-4">
      <AppPageHeader
        app={app}
        title={t('leftItems.title')}
        description={t('leftItems.description')}
        actions={
          <>
            {(state.abandoned_now ?? 0) > 0 && (
              <Button size="sm" variant="danger" disabled={act.isPending}
                      onClick={() => run('acknowledge', {})}>
                <CheckCircle2 size={14} /> Acknowledge all
              </Button>
            )}
            <Button size="sm" variant="outline" onClick={() => statusQuery.refetch()}>
              <RefreshCw size={14} /> Refresh
            </Button>
            {app && (
              <AppConfigureButton app={app} size="sm" variant="outline" label="Configure" />
            )}
          </>
        }
      />

      {/* ── Headline ── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat icon={<UserX size={16} />} value={state.unattended_now ?? 0} label="unattended now"
              tone={(state.unattended_now ?? 0) > 0 ? 'warn' : undefined} />
        <Stat icon={<AlertTriangle size={16} />} value={state.abandoned_now ?? 0}
              label={`past ${mmss(threshold)} alone`}
              tone={(state.abandoned_now ?? 0) > 0 ? 'danger' : undefined} />
        <Stat icon={quiet ? <Moon size={16} /> : <PackageX size={16} />} value={today.alerts}
              label="alerts today" tone={today.alerts > 0 ? 'warn' : undefined} />
        <Stat icon={<UserCheck size={16} />} value={today.reclaimed}
              label={`reclaimed today · ${today.items} items seen`} />
      </div>

      {needsZone.length > 0 && app && (
        <div className="rounded border border-[var(--warn)]/40 bg-[var(--warn)]/5 px-3 py-2 text-sm flex flex-wrap items-center gap-2">
          <PenLine size={14} className="text-[var(--warn)]" />
          <span>
            {needsZone.length === 1 ? 'One camera has no zone drawn:' : `${needsZone.length} cameras have no zone drawn:`}{' '}
            <b>{needsZone.map(cameraName).join(', ')}</b>. The whole frame is watched there, so every bin and planter in shot becomes a candidate.
          </span>
          <AppConfigureButton app={app} variant="link" className="ml-auto text-[var(--accent)] underline" label="Draw it" />
        </div>
      )}

      {/* ── The queue: what is alone right now ── */}
      <Card className={live.some((i) => i.state !== 'unattended') ? 'border-[var(--danger)]/50' : undefined}>
        <CardHeader>
          <UserX size={16} className="text-[var(--text-dim)]" />
          <CardTitle>Alone now</CardTitle>
          <span className="text-xs text-[var(--text-dim)]">{policy}</span>
          {statusQuery.isFetching && <span className="ml-auto text-xs text-[var(--text-dim)]">updating…</span>}
        </CardHeader>
        <CardContent>
          {statusQuery.isPending ? (
            <Skeleton className="h-20" />
          ) : live.length === 0 ? (
            <div className="text-sm text-[var(--text-dim)] py-3 text-center">
              Nothing is sitting on its own. Items with somebody beside them are below.
            </div>
          ) : (
            <ul className="divide-y divide-[var(--border)]">
              {live.map((i) => {
                const ui = stateUi(i.state)
                const bar = i.state === 'unattended'
                  ? (i.progress >= 0.75 ? 'var(--warn)' : 'var(--accent)')
                  : 'var(--danger)'
                const key = `${i.camera}/${i.track}`
                return (
                  <li key={key} className="py-2.5 space-y-1.5">
                    <div className="flex flex-wrap items-center gap-2 text-sm">
                      <Badge variant={ui.variant} title={ui.hint}>{ui.label}</Badge>
                      <span className="font-medium truncate max-w-[12rem]">{cameraName(i.camera)}</span>
                      <span className="text-[var(--text-dim)]">{i.label}</span>
                      <span className="text-xs text-[var(--text-dim)]">
                        {i.owner
                          ? `left by track ${i.owner}${i.owner_left_s ? `, ${mmss(i.owner_left_s)} ago` : ''}`
                          : 'nobody was near it when it appeared'}
                      </span>
                      {i.acked && <Badge variant="info" title="An operator has this one.">acknowledged</Badge>}
                      <span className="ml-auto tabular-nums font-semibold" title="How long it has been alone">
                        {mmss(i.alone_s)}
                      </span>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <div className="flex-1 min-w-[10rem]">
                        <div className="h-2 rounded bg-[var(--bg-2)] overflow-hidden"
                             title={`${Math.round(Math.min(1, i.progress) * 100)}% of the threshold`}>
                          <div className="h-full rounded transition-[width]"
                               style={{ width: `${Math.round(Math.min(1, i.progress) * 100)}%`, background: bar }} />
                        </div>
                      </div>
                      {!i.acked && (
                        <Button size="sm" variant="outline" disabled={act.isPending}
                                title="Somebody is dealing with it — stops the escalation, keeps it on the page."
                                onClick={() => run('acknowledge', { camera: i.camera, track: i.track })}>
                          <Eye size={14} /> Acknowledge
                        </Button>
                      )}
                      <Button size="sm" variant="outline" disabled={act.isPending}
                              title="Collected, removed, or checked and harmless."
                              onClick={() => run('resolve', { camera: i.camera, track: i.track })}>
                        <CheckCircle2 size={14} /> Resolve
                      </Button>
                      {confirmFixture === key ? (
                        <span className="flex items-center gap-1 text-xs">
                          <span className="text-[var(--text-dim)]">Never alert on this spot?</span>
                          <Button size="sm" variant="danger" disabled={act.isPending}
                                  onClick={() => run('mark_fixture', { camera: i.camera, track: i.track })}>
                            Yes, it is a fixture
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => setConfirmFixture(null)}>Cancel</Button>
                        </span>
                      ) : (
                        <Button size="sm" variant="ghost" onClick={() => setConfirmFixture(key)}
                                title="A bin, a planter, a pallet that lives here — remember the spot and stop alerting on it.">
                          <Armchair size={14} /> Fixture
                        </Button>
                      )}
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* ── The normal case ── */}
      {settled.length > 0 && (
        <Card>
          <CardHeader>
            <UserCheck size={16} className="text-[var(--text-dim)]" />
            <CardTitle>Also being followed</CardTitle>
            <span className="text-xs text-[var(--text-dim)]">
              with somebody, or still moving — not counting down. The clock starts{' '}
              {mmss(state.owner_grace_seconds)} after the last person leaves.
            </span>
          </CardHeader>
          <CardContent>
            <ul className="divide-y divide-[var(--border)] text-sm">
              {settled.map((i) => {
                const ui = stateUi(i.state)
                return (
                  <li key={`${i.camera}/${i.track}`} className="flex flex-wrap items-center gap-2 py-1.5">
                    <Badge variant={ui.variant} title={ui.hint}>{ui.label}</Badge>
                    <span className="truncate max-w-[12rem]">{cameraName(i.camera)}</span>
                    <span className="text-[var(--text-dim)]">{i.label}</span>
                    {i.owner && <span className="text-xs text-[var(--text-dim)]">with track {i.owner}</span>}
                    <span className="ml-auto text-xs text-[var(--text-dim)] tabular-nums">
                      there {mmss(i.settled_s)}
                    </span>
                  </li>
                )
              })}
            </ul>
          </CardContent>
        </Card>
      )}

      {/* ── Per camera ── */}
      {statusQuery.isPending ? (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
          <Skeleton className="h-36" /><Skeleton className="h-36" /><Skeleton className="h-36" />
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<Briefcase size={24} />}
          title="No cameras selected"
          description="Select cameras for Abandoned Object and draw each one's zone (Configure, top right). Items are followed within a few seconds."
          action={app ? <AppConfigureButton app={app} variant="primary" label="Configure" /> : undefined}
        />
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
          {rows.map((r) => (
            <Card key={r.camera}>
              <CardHeader>
                <Briefcase size={16} className="text-[var(--text-dim)]" />
                <CardTitle>{cameraName(r.camera)}</CardTitle>
                {r.unattended > 0 && <Badge variant="warning">{r.unattended} alone</Badge>}
                <span className="ml-auto text-xs text-[var(--text-dim)]">{ago(r.last)}</span>
              </CardHeader>
              <CardContent>
                <div className="text-xs text-[var(--text-dim)] mb-2 truncate" title={r.zone}>
                  {r.drawn ? r.zone : 'no zone drawn — whole frame'}
                </div>
                <div className="flex flex-wrap gap-5 mb-2">
                  <Mini value={r.items} label="items now" />
                  <Mini value={r.items_today} label="seen today" />
                  <Mini value={r.alerts_today} label="alerts" tone={r.alerts_today > 0 ? 'warn' : undefined} />
                  <Mini value={r.reclaimed_today} label="reclaimed" />
                </div>
                {r.fixtures > 0 && (
                  <div className="flex items-center gap-2 text-xs text-[var(--text-dim)]">
                    <Armchair size={13} />
                    {r.fixtures} {r.fixtures === 1 ? 'spot' : 'spots'} marked as fixtures
                    <Button size="sm" variant="ghost" className="ml-auto" disabled={act.isPending}
                            onClick={() => run('clear_fixtures', { camera: r.camera })}>
                      Clear
                    </Button>
                  </div>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-5 gap-4">
        {/* ── Recent ── */}
        <Card className="xl:col-span-2">
          <CardHeader><CardTitle>Recent</CardTitle></CardHeader>
          <CardContent>
            {recent.length === 0 ? (
              <div className="text-sm text-[var(--text-dim)] py-4 text-center">Nothing yet.</div>
            ) : (
              <ul className="divide-y divide-[var(--border)] text-sm">
                {recent.map((c, i) => (
                  <li key={i} className="flex items-center gap-2 py-1.5">
                    {c.level && c.level !== 'info'
                      ? <AlertTriangle size={14} className="text-[var(--danger)] shrink-0" />
                      : <Briefcase size={14} className="text-[var(--text-dim)] shrink-0" />}
                    <span className="truncate">
                      {c.message && c.camera
                        ? c.message.split(c.camera).join(cameraName(c.camera))
                        : (c.message ?? '')}
                    </span>
                    <span className="ml-auto text-xs text-[var(--text-dim)] shrink-0">{ago(c.time)}</span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        {/* ── Alarms with evidence ── */}
        <div className="xl:col-span-3">
          <AlarmsTable
            caption="Unattended-item alarms"
            rows={alarmRows}
            cameraLabel={(handle) => (handle ? cameraName(handle) : '—')}
            onAck={(ids) => ack.mutate(ids)}
            ackPending={ack.isPending}
            isPending={alarms.isPending}
            isFetching={alarms.isFetching}
            isError={alarms.isError}
            error={alarms.error}
            emptyTitle="No unattended-item alarms"
            emptyDescription="Alarms appear here with a snapshot from the camera as they fire. If nothing ever fires, widen the zone or check that the item classes match what your detector emits."
            fillHeight={false}
            footer={
              <div className="text-xs text-[var(--text-dim)] px-1 py-1">
                Longest alone today {mmss(state.longest_alone_s)} · settles after {mmss(state.settle_seconds)} ·
                {' '}{state.fixtures ?? 0} fixture {state.fixtures === 1 ? 'spot' : 'spots'} marked.
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