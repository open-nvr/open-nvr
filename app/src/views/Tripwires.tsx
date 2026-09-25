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

// Tripwires — counts and alarms per drawn line. Capability-keyed on
// manifest `provides` ("crossings"); today the Line Crossing app.
//
// Three sources, no data of its own:
//   * the app's /state — today's tallies, the last 24 h per hour, the
//     recent crossings, which cameras still need a line;
//   * core's footfall history (occupancy.footfall.v1 rows the app
//     publishes) — the last 7 days, which outlive the app's process;
//   * the alert inbox, filtered to this app — alarms with their snapshot.

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle, ArrowDownLeft, ArrowUpRight, Download, GitCommitHorizontal,
  Moon, PenLine, RefreshCw,
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
import { AppNoCamerasBanner } from './apps/AppSetup'

export const CROSSINGS_CAPABILITY = 'crossings'
const SOURCE = 'line-crossing'

export function findCrossingsApp(apps: RegisteredApp[] | undefined): RegisteredApp | null {
  return (
    (apps ?? []).find(
      (a) => a.enabled && ((a.manifest as any)?.provides ?? []).includes(CROSSINGS_CAPABILITY)
    ) ?? null
  )
}

type Bucket = { t: number; a_to_b: number; b_to_a: number }
type CameraRow = {
  camera: string
  line: string
  count_direction?: string | null
  today_in: number
  today_out: number
  net: number
  total: number
  last?: number | null
}
type Crossing = {
  time: number
  camera?: string
  label?: string
  direction?: string
  word?: string
  alerted?: boolean
  message?: string
}
type TripwireState = {
  today?: { a_to_b: number; b_to_a: number; net: number; since?: string }
  total_crossings?: number
  alerts_today?: number
  labels?: { a_to_b: string; b_to_a: string }
  alert_mode?: string
  alerts_active_now?: boolean
  per_camera?: CameraRow[]
  hourly?: Record<string, Bucket[]>
  needs_line?: string[]
  recent?: Crossing[]
}
type FootfallResp = {
  hours: number
  cameras: { camera_id: number; entries: number; exits: number; hours: { t: string; entries: number; exits: number }[] }[]
  totals: { entries: number; exits: number }
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

const csvCell = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`

/* ----------------------------- Page ----------------------------- */

export function Tripwires() {
  const { t } = useTranslation()
  const [windowDays, setWindowDays] = useState<1 | 7>(1)

  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
  })
  const app = findCrossingsApp(appsQuery.data)

  const statusQuery = useQuery({
    queryKey: ['app-status', app?.id],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(app!.id)
      return data as { state?: TripwireState }
    },
    enabled: Boolean(app),
    retry: 0,
    refetchInterval: 5000,
  })
  const state: TripwireState = statusQuery.data?.state ?? {}

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

  // Last 7 days from the platform's footfall history — what the app
  // publishes, summed per camera-hour by core, 90-day retention.
  const historyQuery = useQuery({
    queryKey: ['tripwire-footfall', 168],
    queryFn: async () => {
      const { data } = await api.get('/api/v1/occupancy/footfall', { params: { hours: 168 } })
      return data as FootfallResp
    },
    enabled: Boolean(app),
    retry: 0,
    refetchInterval: 60_000,
  })

  const labels = state.labels ?? { a_to_b: 'in', b_to_a: 'out' }
  const rows = state.per_camera ?? []
  const recent = [...(state.recent ?? [])].reverse().slice(0, 20)
  const needsLine = state.needs_line ?? []

  // Per camera, the series the chart shows for the chosen window.
  const seriesFor = (handle: string): { t: number; in: number; out: number }[] => {
    if (windowDays === 1) {
      return (state.hourly?.[handle] ?? []).map((b) => ({ t: b.t, in: b.a_to_b, out: b.b_to_a }))
    }
    const id = idFromHandle(handle)
    const cam = historyQuery.data?.cameras.find((c) => c.camera_id === id)
    // Hour rows → one bar per day, so a week reads at a glance.
    const byDay = new Map<string, { t: number; in: number; out: number }>()
    for (const h of cam?.hours ?? []) {
      const d = new Date(h.t)
      const key = d.toISOString().slice(0, 10)
      const cur = byDay.get(key) ?? { t: new Date(key).getTime() / 1000, in: 0, out: 0 }
      cur.in += h.entries; cur.out += h.exits
      byDay.set(key, cur)
    }
    return Array.from(byDay.values()).sort((a, b) => a.t - b.t)
  }

  const weekTotals = useMemo(() => {
    const cams = historyQuery.data?.cameras ?? []
    return {
      in: cams.reduce((s, c) => s + c.entries, 0),
      out: cams.reduce((s, c) => s + c.exits, 0),
    }
  }, [historyQuery.data])

  const exportCsv = () => {
    const lines = [['camera', 'hour', labels.a_to_b, labels.b_to_a].map(csvCell).join(',')]
    for (const cam of historyQuery.data?.cameras ?? []) {
      const handle = `cam${cam.camera_id}`
      for (const h of cam.hours) {
        lines.push([cameraName(handle), h.t, h.entries, h.exits].map(csvCell).join(','))
      }
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = `tripwires-7d-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  // This app's alarms, newest first, with their snapshot.
  const alarms = useAlarmsList({
    queryKeyPrefix: 'tripwire-alarms', sourceName: SOURCE,
    unacked: false, severity: null, page: 1, pageSize: 8, skip: 0,
  })
  const ack = useAckAlarms()
  const alarmRows = alarms.rows.filter((a) => a.source_name === SOURCE)

  if (!appsQuery.isPending && !app) {
    return (
      <section className="space-y-4">
        <PageHeader title={t('tripwires.title')} description={t('tripwires.description')} />
        <EmptyState
          icon={<GitCommitHorizontal size={28} />}
          title={t('tripwires.noApp')}
          description="Install and enable Line Crossing from the App Catalog. It rides the detection stream the platform already produces — no extra model, no GPU — and counts every tracked person or vehicle that crosses a line you draw."
        />
      </section>
    )
  }

  const today = state.today ?? { a_to_b: 0, b_to_a: 0, net: 0 }
  const policy =
    state.alert_mode === 'off' ? 'counting only'
    : state.alert_mode === 'threshold' ? 'alerts at a count threshold'
    : 'alerts on every crossing'

  return (
    <section className="space-y-4">
      <PageHeader
        title={t('tripwires.title')}
        description={t('tripwires.description')}
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
                <Button size="sm" variant="primary"><PenLine size={14} /> Draw lines</Button>
              </Link>
            )}
          </>
        }
      />

      <AppNoCamerasBanner app={app} />

      {/* ── Headline ── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat icon={<ArrowUpRight size={16} />} value={today.a_to_b} label={`${labels.a_to_b} today`} />
        <Stat icon={<ArrowDownLeft size={16} />} value={today.b_to_a} label={`${labels.b_to_a} today`} />
        <Stat icon={<GitCommitHorizontal size={16} />} value={`${today.net >= 0 ? '+' : ''}${today.net}`} label="net today" />
        <Stat
          icon={state.alerts_active_now === false && state.alert_mode !== 'off' ? <Moon size={16} /> : <AlertTriangle size={16} />}
          value={state.alerts_today ?? 0}
          label={state.alerts_active_now === false && state.alert_mode !== 'off' ? 'alerts today · outside alert hours' : `alerts today · ${policy}`}
          tone={(state.alerts_today ?? 0) > 0 ? 'warn' : undefined}
        />
      </div>

      {needsLine.length > 0 && app && (
        <div className="rounded border border-[var(--warn)]/40 bg-[var(--warn)]/5 px-3 py-2 text-sm flex flex-wrap items-center gap-2">
          <PenLine size={14} className="text-[var(--warn)]" />
          <span>
            {needsLine.length === 1 ? 'One camera is selected but has no line yet:' : `${needsLine.length} cameras are selected but have no line yet:`}{' '}
            <b>{needsLine.map(cameraName).join(', ')}</b>. Nothing is counted there until one is drawn.
          </span>
          <Link to={`/app-catalog/${app.id}`} className="ml-auto text-[var(--accent)] underline">Draw it</Link>
        </div>
      )}

      {/* ── Per camera ── */}
      {statusQuery.isPending ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3"><Skeleton className="h-40" /><Skeleton className="h-40" /></div>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<GitCommitHorizontal size={24} />}
          title="No cameras selected"
          description="Select cameras for Line Crossing and draw each one's line (App Catalog → Line Crossing → Configure). The app starts counting within a few seconds."
        />
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {rows.map((r) => (
            <Card key={r.camera}>
              <CardHeader>
                <GitCommitHorizontal size={16} className="text-[var(--text-dim)]" />
                <CardTitle>{cameraName(r.camera)}</CardTitle>
                <span className="text-xs text-[var(--text-dim)]">{r.line}</span>
                {r.count_direction && r.count_direction !== 'both' && (
                  <Badge variant="neutral">{r.count_direction === 'a_to_b' ? `${labels.a_to_b} only` : `${labels.b_to_a} only`}</Badge>
                )}
                <span className="ml-auto text-xs text-[var(--text-dim)]">last {ago(r.last)}</span>
              </CardHeader>
              <CardContent>
                {r.line.startsWith('—') ? (
                  <div className="text-sm text-[var(--text-dim)] py-4 text-center">No line drawn — nothing is counted on this camera yet.</div>
                ) : (
                  <>
                    <div className="flex gap-5 mb-2">
                      <Mini value={r.today_in} label={labels.a_to_b} tone="in" />
                      <Mini value={r.today_out} label={labels.b_to_a} tone="out" />
                      <Mini value={`${r.net >= 0 ? '+' : ''}${r.net}`} label="net" />
                      <Mini value={r.total} label="since start" dim />
                    </div>
                    <FlowBars series={seriesFor(r.camera)} daily={windowDays === 7} labels={labels} />
                  </>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-5 gap-4">
        {/* ── Recent crossings ── */}
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Recent crossings</CardTitle>
            {statusQuery.isFetching && <span className="ml-auto text-xs text-[var(--text-dim)]">updating…</span>}
          </CardHeader>
          <CardContent>
            {recent.length === 0 ? (
              <div className="text-sm text-[var(--text-dim)] py-4 text-center">Nothing has crossed yet.</div>
            ) : (
              <ul className="divide-y divide-[var(--border)] text-sm">
                {recent.map((c, i) => (
                  <li key={i} className="flex items-center gap-2 py-1.5">
                    {c.direction === 'a_to_b'
                      ? <ArrowUpRight size={14} className="text-[var(--ok)] shrink-0" />
                      : <ArrowDownLeft size={14} className="text-[var(--text-dim)] shrink-0" />}
                    <span className="font-medium">{c.label ?? 'object'} {c.word ?? c.direction}</span>
                    <span className="text-[var(--text-dim)] truncate">{c.camera ? cameraName(c.camera) : ''}</span>
                    {c.alerted && <Badge variant="destructive">alert</Badge>}
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
            caption="Tripwire alarms"
            rows={alarmRows}
            cameraLabel={(handle) => (handle ? cameraName(handle) : '—')}
            onAck={(ids) => ack.mutate(ids)}
            ackPending={ack.isPending}
            isPending={alarms.isPending}
            isFetching={alarms.isFetching}
            isError={alarms.isError}
            error={alarms.error}
            emptyTitle="No tripwire alarms"
            emptyDescription={state.alert_mode === 'off'
              ? 'The app is counting only. Switch alert_mode to every or threshold in its config to raise alarms.'
              : 'Alarms appear here with a snapshot from the camera as they fire.'}
            fillHeight={false}
            footer={
              <div className="text-xs text-[var(--text-dim)] px-1 py-1">
                Last 7 days across all lines: {weekTotals.in} {labels.a_to_b} · {weekTotals.out} {labels.b_to_a}.
                {' '}<Link to="/alarms" className="text-[var(--accent)] underline">All alarms</Link>
              </div>
            }
          />
        </div>
      </div>
    </section>
  )
}

/* ---------------------------- Pieces ---------------------------- */

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

function Mini({ value, label, tone, dim }: { value: React.ReactNode; label: string; tone?: 'in' | 'out'; dim?: boolean }) {
  const color = tone === 'in' ? 'text-[var(--ok)]' : dim ? 'text-[var(--text-dim)]' : ''
  return (
    <div>
      <div className={`text-lg font-semibold leading-none ${color}`}>{value}</div>
      <div className="text-[11px] text-[var(--text-dim)] mt-0.5">{label}</div>
    </div>
  )
}

/** Mirrored bars: `in` above the axis, `out` below — the same idiom
 *  the Occupancy page uses for footfall, so a person who knows one
 *  reads the other. */
function FlowBars({ series, daily, labels }: {
  series: { t: number; in: number; out: number }[]
  daily: boolean
  labels: { a_to_b: string; b_to_a: string }
}) {
  const dfmt = useDateFormat()
  if (series.length === 0) {
    return <div className="text-xs text-[var(--text-dim)] py-3 text-center">No history yet for this window.</div>
  }
  const top = Math.max(1, ...series.map((s) => Math.max(s.in, s.out)))
  const W = 480, H = 96, PAD = 4, LABEL_H = 14, MID = (H - LABEL_H) / 2
  const bw = (W - 2 * PAD) / series.length
  const scale = (v: number) => (v / top) * (MID - PAD)
  const fmt = (t: number) => {
    const d = new Date(t * 1000)
    return daily ? dfmt.date(d, { weekday: 'short' }) : dfmt.time(d, { hour: '2-digit' })
  }
  const every = Math.max(1, Math.ceil(series.length / 8))
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: H }} role="img"
         aria-label={`${labels.a_to_b} above the line and ${labels.b_to_a} below it, per ${daily ? 'day' : 'hour'}`}>
      <line x1={PAD} x2={W - PAD} y1={MID} y2={MID} stroke="var(--border,#333)" strokeWidth="1" />
      {series.map((s, i) => {
        const x = PAD + i * bw + 1
        const up = scale(s.in), down = scale(s.out)
        return (
          <g key={s.t}>
            {up > 0 && <rect x={x} y={MID - up} width={Math.max(1, bw - 2)} height={up} rx="2" fill="var(--accent,#3b82f6)" fillOpacity="0.85" />}
            {down > 0 && <rect x={x} y={MID + 1} width={Math.max(1, bw - 2)} height={down} rx="2" fill="var(--text-dim,#6b7280)" fillOpacity="0.55" />}
            <rect x={x} y={0} width={bw} height={H - LABEL_H} fill="transparent">
              <title>{`${fmt(s.t)} · ${s.in} ${labels.a_to_b} · ${s.out} ${labels.b_to_a}`}</title>
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
