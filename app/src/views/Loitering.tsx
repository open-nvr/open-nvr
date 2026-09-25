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

// Loitering — the first-class page for the loitering-detection app.
// Who is dwelling in a zone right now and how close they are to the
// threshold, each camera's stays / alerts / longest / average today with
// a 24-hour strip and a dwell-length histogram, the last week from the
// platform's footfall history (dwell fields), the recent stays, and the
// app's alarms with their snapshots. The app's live state is read from
// its /state; a dweller can be dismissed through its `dismiss` action.

import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Download, Hourglass, Moon, PenLine, RefreshCw, Timer, UserCheck, Users,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { apiService } from '../lib/apiService'
import { useTranslation, useDateFormat, type DateFormatters } from '../i18n'
import {
  Badge, Button, Card, CardContent, CardHeader, CardTitle,
  EmptyState, PageHeader, Skeleton,
} from '../components/ui'
import { AlarmsTable } from '../components/alarms/AlarmsTable'
import { useAckAlarms, useAlarmsList } from '../components/alarms/useAlarmsList'
import type { RegisteredApp } from './AppCatalog'
import { AppPageHeader } from './apps/AppSetup'

export const LOITERING_CAPABILITY = 'loitering'
const SOURCE = 'loitering-detection'

export function findLoiteringApp(apps: RegisteredApp[] | undefined): RegisteredApp | null {
  return (
    (apps ?? []).find(
      (a) => a.enabled && ((a.manifest as any)?.provides ?? []).includes(LOITERING_CAPABILITY)
    ) ?? null
  )
}

type Bucket = { t: number; stays: number; alerts: number }
type CameraRow = {
  camera: string
  zone: string
  drawn: boolean
  dwelling: number
  stays_today: number
  alerts_today: number
  longest_s: number
  avg_s: number
  histogram: { bucket: string; n: number }[]
  total: number
  last?: number | null
}
type Dweller = {
  camera: string
  label: string
  track: string
  dwell_s: number
  stage: 'watching' | 'alerted' | 'escalated' | 'dismissed' | string
  progress: number
}
type Recent = {
  time: number
  message?: string
  level?: string
  camera?: string
  label?: string
  track?: string
  dwell_s?: number
  stage?: string
}
type LoiteringState = {
  dwelling_now?: number
  today?: { stays: number; alerts: number; longest_s: number; since?: string }
  threshold_seconds?: number
  threshold_now?: number
  alerts_active_now?: boolean
  after_hours?: boolean
  escalate_after_seconds?: number
  group_size?: number
  per_camera?: CameraRow[]
  dwelling?: Dweller[]
  hourly?: Record<string, Bucket[]>
  needs_zone?: string[]
  recent?: Recent[]
}
type FootfallResp = {
  hours: number
  cameras: {
    camera_id: number
    dwell_count: number
    dwell_seconds: number
    dwell_max_seconds: number
    dwell_avg_seconds: number | null
    hours: { t: string; dwell_count?: number; dwell_max_seconds?: number; dwell_avg_seconds: number | null }[]
  }[]
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

/** 75 → "1:15", 12 → "12s", 0 → "0s". */
function mmss(s: number | null | undefined): string {
  const v = Math.max(0, Math.floor(s ?? 0))
  if (v < 60) return `${v}s`
  return `${Math.floor(v / 60)}:${String(v % 60).padStart(2, '0')}`
}

const csvCell = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`

const STAGE_TONE: Record<string, { variant: 'destructive' | 'warning' | 'neutral' | 'success'; text: string }> = {
  watching: { variant: 'neutral', text: 'watching' },
  alerted: { variant: 'destructive', text: 'alerted' },
  escalated: { variant: 'destructive', text: 'escalated' },
  dismissed: { variant: 'success', text: 'dismissed' },
}

/* ----------------------------- Page ----------------------------- */

export function Loitering() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [windowDays, setWindowDays] = useState<1 | 7>(1)

  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
  })
  const app = findLoiteringApp(appsQuery.data)

  const statusQuery = useQuery({
    queryKey: ['app-status', app?.id],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(app!.id)
      return data as { state?: LoiteringState }
    },
    enabled: Boolean(app),
    retry: 0,
    refetchInterval: 3000,
  })
  const state: LoiteringState = statusQuery.data?.state ?? {}

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

  // Last 7 days of finished stays from the platform's footfall history —
  // the dwell fields this app publishes, summed per camera-hour by core.
  const historyQuery = useQuery({
    queryKey: ['loitering-dwell', 168],
    queryFn: async () => {
      const { data } = await api.get('/api/v1/occupancy/footfall', { params: { hours: 168 } })
      return data as FootfallResp
    },
    enabled: Boolean(app),
    retry: 0,
    refetchInterval: 60_000,
  })

  const dismiss = useMutation({
    mutationFn: async (d: Dweller) =>
      apiService.invokeAppAction(app!.id, 'dismiss', { camera: d.camera, track: d.track }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['app-status', app?.id] }),
  })

  const rows = state.per_camera ?? []
  const dwelling = state.dwelling ?? []
  const recent = [...(state.recent ?? [])].reverse().slice(0, 20)
  const needsZone = state.needs_zone ?? []
  const threshold = state.threshold_now ?? state.threshold_seconds ?? 60

  // Per camera, the series the strip shows for the chosen window.
  const seriesFor = (handle: string): Bucket[] => {
    if (windowDays === 1) return state.hourly?.[handle] ?? []
    const id = idFromHandle(handle)
    const cam = historyQuery.data?.cameras.find((c) => c.camera_id === id)
    const byDay = new Map<string, Bucket>()
    for (const h of cam?.hours ?? []) {
      const key = new Date(h.t).toISOString().slice(0, 10)
      const cur = byDay.get(key) ?? { t: new Date(key).getTime() / 1000, stays: 0, alerts: 0 }
      cur.stays += h.dwell_count ?? 0
      byDay.set(key, cur)
    }
    return Array.from(byDay.values()).sort((a, b) => a.t - b.t)
  }

  const week = useMemo(() => {
    const cams = historyQuery.data?.cameras ?? []
    const stays = cams.reduce((s, c) => s + (c.dwell_count ?? 0), 0)
    const seconds = cams.reduce((s, c) => s + (c.dwell_seconds ?? 0), 0)
    const longest = cams.reduce((m, c) => Math.max(m, c.dwell_max_seconds ?? 0), 0)
    return { stays, avg: stays ? seconds / stays : 0, longest }
  }, [historyQuery.data])

  const exportCsv = () => {
    const lines = [['camera', 'hour', 'stays', 'avg_dwell_s', 'longest_s'].map(csvCell).join(',')]
    for (const cam of historyQuery.data?.cameras ?? []) {
      const handle = `cam${cam.camera_id}`
      for (const h of cam.hours) {
        lines.push([cameraName(handle), h.t, h.dwell_count ?? 0, h.dwell_avg_seconds ?? '', h.dwell_max_seconds ?? ''].map(csvCell).join(','))
      }
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = `loitering-7d-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const alarms = useAlarmsList({
    queryKeyPrefix: 'loitering-alarms', sourceName: SOURCE,
    unacked: false, severity: null, page: 1, pageSize: 8, skip: 0,
  })
  const ack = useAckAlarms()
  const alarmRows = alarms.rows.filter((a) => a.source_name === SOURCE)

  if (!appsQuery.isPending && !app) {
    return (
      <section className="space-y-4">
        <PageHeader title={t('loitering.title')} description={t('loitering.description')} />
        <EmptyState
          icon={<Hourglass size={28} />}
          title={t('loitering.noApp')}
          description="Install and enable Loitering Detection from the App Catalog. It rides the detection stream the platform already produces — no extra model, no GPU — and alerts when a tracked person or vehicle stays in a zone you draw longer than you allow."
        />
      </section>
    )
  }

  const today = state.today ?? { stays: 0, alerts: 0, longest_s: 0 }
  const quiet = state.alerts_active_now === false
  const policy = quiet
    ? 'outside alert hours'
    : state.after_hours
      ? `after hours · alert at ${mmss(threshold)}`
      : `alert at ${mmss(threshold)}${state.escalate_after_seconds ? ` · escalate +${mmss(state.escalate_after_seconds)}` : ''}`

  return (
    <section className="space-y-4">
      <AppPageHeader
        app={app}
        title={t('loitering.title')}
        description={t('loitering.description')}
        actions={
          <>
            <div className="flex rounded border border-[var(--border)] overflow-hidden text-xs">
              {([1, 7] as const).map((d) => (
                <button
                  key={d}
                  type="button"
                  className={`px-2.5 py-1.5 ${windowDays === d ? 'bg-[var(--accent)] text-white' : 'bg-[var(--bg-2)] text-[var(--text-dim)] hover:text-[var(--text)]'}`}
                  onClick={() => setWindowDays(d)}
                >
                  {d === 1 ? 'Today, by hour' : '7 days'}
                </button>
              ))}
            </div>
            <Button size="sm" variant="outline" onClick={exportCsv} disabled={!historyQuery.data}>
              <Download size={14} /> CSV
            </Button>
            <Button size="sm" variant="outline" onClick={() => { statusQuery.refetch(); historyQuery.refetch() }}>
              <RefreshCw size={14} /> Refresh
            </Button>
            {app && (
              <Link to={`/app-catalog/${app.id}`}>
                <Button size="sm" variant="primary"><PenLine size={14} /> Draw zones</Button>
              </Link>
            )}
          </>
        }
      />

      {/* ── Headline ── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat icon={<Users size={16} />} value={state.dwelling_now ?? 0} label="dwelling now"
              tone={dwelling.some((d) => d.stage === 'alerted' || d.stage === 'escalated') ? 'warn' : undefined} />
        <Stat icon={<Hourglass size={16} />} value={today.stays} label="stays today" />
        <Stat
          icon={quiet ? <Moon size={16} /> : <AlertTriangle size={16} />}
          value={today.alerts}
          label={`alerts today · ${policy}`}
          tone={today.alerts > 0 ? 'warn' : undefined}
        />
        <Stat icon={<Timer size={16} />} value={mmss(today.longest_s)} label="longest stay today" />
      </div>

      {needsZone.length > 0 && app && (
        <div className="rounded border border-[var(--warn)]/40 bg-[var(--warn)]/5 px-3 py-2 text-sm flex flex-wrap items-center gap-2">
          <PenLine size={14} className="text-[var(--warn)]" />
          <span>
            {needsZone.length === 1 ? 'One camera has no zone drawn:' : `${needsZone.length} cameras have no zone drawn:`}{' '}
            <b>{needsZone.map(cameraName).join(', ')}</b>. The whole frame is watched there, which usually means a threshold that never matters or one that fires on everyone.
          </span>
          <Link to={`/app-catalog/${app.id}`} className="ml-auto text-[var(--accent)] underline">Draw it</Link>
        </div>
      )}

      {/* ── Dwelling now ── */}
      <Card>
        <CardHeader>
          <Users size={16} className="text-[var(--text-dim)]" />
          <CardTitle>Dwelling now</CardTitle>
          <span className="text-xs text-[var(--text-dim)]">threshold {mmss(threshold)}</span>
          {statusQuery.isFetching && <span className="ml-auto text-xs text-[var(--text-dim)]">updating…</span>}
        </CardHeader>
        <CardContent>
          {statusQuery.isPending ? (
            <Skeleton className="h-16" />
          ) : dwelling.length === 0 ? (
            <div className="text-sm text-[var(--text-dim)] py-3 text-center">Nobody is inside a zone right now.</div>
          ) : (
            <ul className="divide-y divide-[var(--border)]">
              {dwelling.map((d) => {
                const tone = STAGE_TONE[d.stage] ?? STAGE_TONE.watching
                const barColor = d.stage === 'alerted' || d.stage === 'escalated' ? 'var(--danger)'
                  : d.stage === 'dismissed' ? 'var(--text-dim)' : d.progress >= 0.75 ? 'var(--warn)' : 'var(--accent)'
                return (
                  <li key={`${d.camera}/${d.track}`} className="flex flex-wrap items-center gap-3 py-2 text-sm">
                    <span className="font-medium min-w-[9rem] truncate">{cameraName(d.camera)}</span>
                    <span className="text-[var(--text-dim)] w-16">{d.label}</span>
                    <div className="flex-1 min-w-[10rem]">
                      <div className="h-2 rounded bg-[var(--bg-2)] overflow-hidden" title={`${Math.round(d.progress * 100)}% of the threshold`}>
                        <div className="h-full rounded transition-[width]" style={{ width: `${Math.round(Math.min(1, d.progress) * 100)}%`, background: barColor }} />
                      </div>
                    </div>
                    <span className="tabular-nums w-14 text-right font-semibold">{mmss(d.dwell_s)}</span>
                    <Badge variant={tone.variant}>{tone.text}</Badge>
                    {d.stage !== 'dismissed' && (
                      <Button size="sm" variant="outline" title="Known person — raise nothing more for this stay"
                              disabled={dismiss.isPending} onClick={() => dismiss.mutate(d)}>
                        <UserCheck size={14} /> Dismiss
                      </Button>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* ── Per camera ── */}
      {statusQuery.isPending ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3"><Skeleton className="h-44" /><Skeleton className="h-44" /></div>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<Hourglass size={24} />}
          title="No cameras selected"
          description="Select cameras for Loitering Detection and draw each one's zone (App Catalog → Loitering Detection → Configure). The app starts measuring within a few seconds."
        />
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {rows.map((r) => (
            <Card key={r.camera}>
              <CardHeader>
                <Hourglass size={16} className="text-[var(--text-dim)]" />
                <CardTitle>{cameraName(r.camera)}</CardTitle>
                <span className="text-xs text-[var(--text-dim)]">{r.drawn ? r.zone : 'whole frame'}</span>
                {r.dwelling > 0 && <Badge variant="warning">{r.dwelling} inside</Badge>}
                <span className="ml-auto text-xs text-[var(--text-dim)]">last seen {ago(r.last)}</span>
              </CardHeader>
              <CardContent>
                <div className="flex flex-wrap gap-5 mb-2">
                  <Mini value={r.stays_today} label="stays today" />
                  <Mini value={r.alerts_today} label="alerts" tone={r.alerts_today > 0 ? 'warn' : undefined} />
                  <Mini value={mmss(r.longest_s)} label="longest" />
                  <Mini value={mmss(r.avg_s)} label="average" />
                  <Mini value={r.total} label="since start" dim />
                </div>
                <StayBars series={seriesFor(r.camera)} daily={windowDays === 7} />
                <Histogram buckets={r.histogram} />
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-5 gap-4">
        {/* ── Recent stays ── */}
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Recent</CardTitle>
          </CardHeader>
          <CardContent>
            {recent.length === 0 ? (
              <div className="text-sm text-[var(--text-dim)] py-4 text-center">No stays yet.</div>
            ) : (
              <ul className="divide-y divide-[var(--border)] text-sm">
                {recent.map((c, i) => (
                  <li key={i} className="flex items-center gap-2 py-1.5">
                    {c.level && c.level !== 'info'
                      ? <AlertTriangle size={14} className="text-[var(--danger)] shrink-0" />
                      : <Hourglass size={14} className="text-[var(--text-dim)] shrink-0" />}
                    <span className="truncate">
                      {c.message && c.camera ? c.message.replace(c.camera, cameraName(c.camera)) : (c.message ?? `${c.label ?? 'object'} at ${c.camera ?? ''}`)}
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
            caption="Loitering alarms"
            rows={alarmRows}
            cameraLabel={(handle) => (handle ? cameraName(handle) : '—')}
            onAck={(ids) => ack.mutate(ids)}
            ackPending={ack.isPending}
            isPending={alarms.isPending}
            isFetching={alarms.isFetching}
            isError={alarms.isError}
            error={alarms.error}
            emptyTitle="No loitering alarms"
            emptyDescription="Alarms appear here with a snapshot from the camera as they fire. Lower threshold_seconds in the app's config if nothing ever fires."
            fillHeight={false}
            footer={
              <div className="text-xs text-[var(--text-dim)] px-1 py-1">
                Last 7 days across all zones: {week.stays} stays · average {mmss(week.avg)} · longest {mmss(week.longest)}.
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

function Stat({ icon, value, label, tone }: { icon: React.ReactNode; value: React.ReactNode; label: string; tone?: 'warn' }) {
  return (
    <Card>
      <CardContent className="flex items-center gap-3 py-3">
        <span className="text-[var(--text-dim)]">{icon}</span>
        <div className="min-w-0">
          <div className={`text-xl font-semibold leading-none ${tone === 'warn' ? 'text-[var(--warn)]' : ''}`}>{value}</div>
          <div className="text-xs text-[var(--text-dim)] mt-1 truncate">{label}</div>
        </div>
      </CardContent>
    </Card>
  )
}

function Mini({ value, label, tone, dim }: { value: React.ReactNode; label: string; tone?: 'warn'; dim?: boolean }) {
  const color = tone === 'warn' ? 'text-[var(--warn)]' : dim ? 'text-[var(--text-dim)]' : ''
  return (
    <div>
      <div className={`text-lg font-semibold leading-none ${color}`}>{value}</div>
      <div className="text-[11px] text-[var(--text-dim)] mt-0.5">{label}</div>
    </div>
  )
}

/** Stays per hour (or per day), alerts overlaid in the danger colour. */
function StayBars({ series, daily }: { series: Bucket[]; daily: boolean }) {
  const dfmt = useDateFormat()
  if (series.length === 0) {
    return <div className="text-xs text-[var(--text-dim)] py-3 text-center">No history yet for this window.</div>
  }
  const top = Math.max(1, ...series.map((s) => s.stays))
  const W = 480, H = 72, PAD = 4, LABEL_H = 14, PLOT = H - LABEL_H - PAD
  const bw = (W - 2 * PAD) / series.length
  const scale = (v: number) => (v / top) * PLOT
  const fmt = (t: number) => {
    const d = new Date(t * 1000)
    return daily ? dfmt.date(d, { weekday: 'short' }) : dfmt.time(d, { hour: '2-digit' })
  }
  const every = Math.max(1, Math.ceil(series.length / 8))
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: H }} role="img"
         aria-label={`Stays per ${daily ? 'day' : 'hour'}, alerts in red`}>
      <line x1={PAD} x2={W - PAD} y1={PAD + PLOT} y2={PAD + PLOT} stroke="var(--border,#333)" strokeWidth="1" />
      {series.map((s, i) => {
        const x = PAD + i * bw + 1
        const h = scale(s.stays), a = scale(s.alerts)
        return (
          <g key={s.t}>
            {h > 0 && <rect x={x} y={PAD + PLOT - h} width={Math.max(1, bw - 2)} height={h} rx="2" fill="var(--accent,#3b82f6)" fillOpacity="0.8" />}
            {a > 0 && <rect x={x} y={PAD + PLOT - a} width={Math.max(1, bw - 2)} height={a} rx="2" fill="var(--danger,#e5484d)" fillOpacity="0.9" />}
            <rect x={x} y={0} width={bw} height={H - LABEL_H} fill="transparent">
              <title>{`${fmt(s.t)} · ${s.stays} stays · ${s.alerts} alerts`}</title>
            </rect>
            {i % every === 0 && (
              <text x={x} y={H - 2} fontSize="9" fill="var(--text-dim,#6b7280)">{fmt(s.t)}</text>
            )}
          </g>
        )
      })}
    </svg>
  )
}

/** Today's stays by length — the tuning view: if most stays sit just
 *  under the threshold the threshold is right; if most sit far above
 *  it, it is too loose. */
function Histogram({ buckets }: { buckets: { bucket: string; n: number }[] }) {
  const top = Math.max(1, ...buckets.map((b) => b.n))
  return (
    <div className="mt-2 flex items-end gap-2" aria-label="Stay length histogram, today">
      {buckets.map((b) => (
        <div key={b.bucket} className="flex-1 text-center" title={`${b.n} stays of ${b.bucket}`}>
          <div className="h-8 flex items-end justify-center">
            <div className="w-full max-w-[28px] rounded-t bg-[var(--text-dim)]/40" style={{ height: `${Math.max(2, (b.n / top) * 100)}%` }} />
          </div>
          <div className="text-[10px] text-[var(--text-dim)] mt-0.5">{b.bucket}</div>
          <div className="text-[10px] tabular-nums">{b.n}</div>
        </div>
      ))}
    </div>
  )
}
