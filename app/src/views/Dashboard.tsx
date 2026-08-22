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

import { useCallback, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Camera, ChartArea, ChartBar, CircleCheck, RefreshCw, AlertTriangle, HardDrive, Play, Info } from 'lucide-react'
import { ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip as RTooltip, CartesianGrid, BarChart, Bar, Cell } from 'recharts'
import SystemNetworkMonitoring from './SystemNetworkMonitoring'
import { Card, CardHeader, CardTitle, CardContent, Badge, Button, Skeleton, ErrorCard, StatusDot } from '../components/ui'
import { extractApiError } from '../lib/apiError'
import { useCameraStatusConnected } from '../hooks/useCameraStatus'
import { useCameras, useRecordingsByDate, useSuricataStats, useSystemResources, type CameraItem } from '../lib/queries'
import { StatTile, UsageBar } from '../components/ui/stats'
import { formatDuration, localDayStart, todayLocalKey } from '../lib/time'

type RecordingItem = { start_time?: string | null; id: number; camera?: string; relpath?: string; url?: string; size?: number }

/** "Today" / "Sat, Aug 15" for a local YYYY-MM-DD date key. */
function fmtDay(date: string): string {
  if (date === todayLocalKey()) return 'Today'
  return new Date(localDayStart(date)).toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  })
}

function KpiCard({ icon, label, value, help, tone = 'neutral', onClick }: { icon: React.ReactNode; label: string; value: string | number; help?: string; tone?: 'neutral' | 'success' | 'warning' | 'destructive'; onClick?: () => void }) {
  const toneCls = {
    neutral: 'text-slate-300',
    success: 'text-emerald-300',
    warning: 'text-amber-300',
    destructive: 'text-red-300',
  } as const

  const CardComponent = onClick ? 'button' : 'div'

  return (
    <Card className={onClick ? 'cursor-pointer hover:bg-[var(--panel)] transition-colors' : ''}>
      <CardComponent
        onClick={onClick}
        className={onClick ? 'w-full text-left' : ''}
      >
        <CardHeader>
          <div className={`p-2 rounded-md bg-[var(--bg-2)] ${toneCls[tone]}`}>{icon}</div>
          <div className="ml-2">
            <div className="text-xs uppercase tracking-wide text-[var(--text-dim)]">{label}</div>
            <div className="text-xl font-semibold text-[var(--text)]">{value}</div>
          </div>
        </CardHeader>
        {help && (
          <CardContent>
            <div className="text-xs text-[var(--text-dim)] flex items-center gap-1"><Info size={12} /> {help}</div>
          </CardContent>
        )}
      </CardComponent>
    </Card>
  )
}

function CameraTile({ cam, status, recording }: { cam: CameraItem; status: 'online' | 'offline' | 'degraded' | 'error'; recording?: boolean }) {
  return (
    <div className="aspect-video rounded-lg border border-[var(--border)] bg-[var(--bg-2)] relative overflow-hidden">
      <div className="absolute left-2 top-2 text-xs text-[var(--text)] flex items-center gap-2">
        <StatusDot status={status} />
        <span className="font-medium">{cam.name || `Camera ${cam.id}`}</span>
      </div>
      <div className="absolute right-2 top-2 flex items-center gap-2">
        {recording ? <Badge variant="warning">REC</Badge> : null}
        <Badge variant="neutral">{cam.ip_address}</Badge>
      </div>
      <div className="absolute left-2 bottom-2 text-[10px] text-[var(--text-dim)]">ID: {cam.id}</div>
      <div className="absolute right-2 bottom-2">
        <Link to="/live" className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-[var(--panel)] border border-[var(--border)] hover:bg-[var(--panel-2)]">
          <Play size={12} /> Open
        </Link>
      </div>
    </div>
  )
}

export function Dashboard() {
  const navigate = useNavigate()

  const camsQuery = useCameras()
  const recsQuery = useRecordingsByDate()
  const alertsQuery = useSuricataStats()

  const cams = camsQuery.data?.cameras ?? null
  const camsTotal = camsQuery.data?.total ?? cams?.length ?? 0
  const camsErr = camsQuery.isError ? extractApiError(camsQuery.error, 'Failed to load cameras') : null
  const loadingCams = camsQuery.isPending

  // Flatten per-camera daily recordings for the chart
  const recs = useMemo(() => {
    if (!recsQuery.data) return null
    const dailyRecs: RecordingItem[] = []
    for (const cam of recsQuery.data.cameras || []) {
      for (const rec of cam.recordings || []) {
        dailyRecs.push({ id: 0, start_time: rec.date, camera: cam.camera_name })
      }
    }
    return dailyRecs
  }, [recsQuery.data])
  // total_recordings is a camera-day count (1 per camera per day) — misleading
  // as a KPI, so the tile shows total footage duration instead.
  const recsDuration = recsQuery.data?.total_duration ?? 0
  const recsErr = recsQuery.isError ? extractApiError(recsQuery.error, 'Failed to load recordings') : null
  const loadingRecs = recsQuery.isPending

  const alertsHigh = alertsQuery.data?.by_severity?.['1'] ?? 0
  const alertsErr = alertsQuery.isError ? extractApiError(alertsQuery.error, 'No alert endpoint configured') : null
  const loadingAlerts = alertsQuery.isPending

  const refreshing = camsQuery.isFetching || recsQuery.isFetching || alertsQuery.isFetching
  const liveUpdates = useCameraStatusConnected()

  const refreshAll = useCallback(async () => {
    await Promise.all([camsQuery.refetch(), recsQuery.refetch(), alertsQuery.refetch()])
  }, [camsQuery.refetch, recsQuery.refetch, alertsQuery.refetch])

  // Camera liveness rides in on the camera list itself, which refreshes on its
  // own interval and whenever the events socket reports a transition. This
  // used to be a per-camera /mediamtx-status fan-out fired once, 500ms after
  // the list arrived — so the KPI and the chart below were pinned to whatever
  // was true when the page opened, and the "Polling" toggle that was supposed
  // to fix that refetched the queries without ever re-running the fan-out.
  const onlineCount = useMemo(
    () => (cams ?? []).filter((c) => c.live_online === true).length,
    [cams],
  )

  const statusOf = useCallback((c: CameraItem): 'online' | 'offline' | 'degraded' | 'error' => {
    // Check camera.status for error/failed first
    if (c.status && ['error', 'failed'].includes(c.status)) return 'error'

    if (c.live_online === true) return 'online'
    if (c.live_online === false) return 'offline'

    // Liveness unknown — the recorder restarted and has not re-seeded, or this
    // camera has never been seen. Provisioned-but-unknown is degraded, not
    // offline, so a restart doesn't briefly report the fleet as down.
    if (c.status === 'provisioned' || c.status === 'active') return 'degraded'

    // Offline = camera not provisioned or no path/config
    return 'offline'
  }, [])

  // Charts data
  const recordingsByDay = useMemo(() => {
    const map = new Map<string, number>()
    for (const r of recs || []) {
      const dt = r.start_time ? new Date(r.start_time) : null
      if (!dt) continue
      const key = dt.toISOString().slice(0, 10)
      map.set(key, (map.get(key) || 0) + 1)
    }
    return Array.from(map.entries())
      .sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([day, count]) => ({ day, count }))
  }, [recs])

  // Per-camera recording availability for the breakdown table.
  const recsByCamera = useMemo(() => {
    return (recsQuery.data?.cameras || []).map((c) => {
      const days = c.recordings ?? []
      const latest = days.reduce((m, r) => (r.date > m ? r.date : m), '')
      return {
        id: c.camera_id ?? 0,
        name: c.camera_name || `Camera ${c.camera_id}`,
        days: days.length,
        duration: c.total_duration ?? days.reduce((s, r) => s + (r.total_duration || 0), 0),
        latest: latest || null,
      }
    })
  }, [recsQuery.data])

  const camerasByStatus = useMemo(() => {
    const agg = { online: 0, degraded: 0, offline: 0, error: 0 }
    for (const c of cams || []) {
      agg[statusOf(c)]++
    }
    return [
      { name: 'Online', value: agg.online },
      { name: 'Degraded', value: agg.degraded },
      { name: 'Offline', value: agg.offline },
      { name: 'Error', value: agg.error },
    ]
  }, [cams, statusOf])

  return (
    <section className="space-y-4">
      {/* Header actions */}
      <div className="flex items-center gap-2">
        <h1 className="text-lg font-semibold">Dashboard</h1>
        <div className="ml-auto flex items-center gap-2">
          {/* Camera status arrives over the events socket and no longer needs
              a manual poll. Say so when that socket is down, since the cards
              are then only as fresh as the list's own refetch interval. */}
          {!liveUpdates && (
            <span className="text-xs text-[var(--text-dim)]" title="Reconnecting to the live event stream; status may lag by up to a minute">
              Live updates reconnecting…
            </span>
          )}
          <Button onClick={refreshAll} disabled={refreshing}><RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} /> Refresh</Button>
        </div>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {loadingCams ? (
          <Skeleton className="h-24" />
        ) : camsErr ? (
          <ErrorCard title="Cameras" message={camsErr} onRetry={() => camsQuery.refetch()} />
        ) : (
          <KpiCard
            icon={<Camera size={18} />}
            label="Cameras"
            value={camsTotal}
            help="Total active cameras"
            onClick={() => navigate('/cameras')}
          />
        )}

        <KpiCard icon={<CircleCheck size={18} />} label="Online" value={loadingCams ? '—' : onlineCount} tone="success" />

        {loadingRecs ? (
          <Skeleton className="h-24" />
        ) : recsErr ? (
          <ErrorCard title="Recordings" message={recsErr} onRetry={() => recsQuery.refetch()} />
        ) : (
          <KpiCard
            icon={<HardDrive size={18} />}
            label="Recordings"
            value={formatDuration(recsDuration)}
            help={`Footage from ${recsByCamera.filter((c) => c.days > 0).length} of ${camsTotal} cameras`}
            onClick={() => navigate('/playback')}
          />
        )}

        {loadingAlerts ? (
          <Skeleton className="h-24" />
        ) : alertsErr ? (
          <KpiCard icon={<AlertTriangle size={18} />} label="Alerts" value={0} help={alertsErr || 'No alert endpoint configured'} />
        ) : (
          <KpiCard
            icon={alertsHigh > 0 ? <AlertTriangle size={18} /> : <CircleCheck size={18} />}
            label="Alerts"
            value={alertsHigh}
            help="High severity alerts"
            tone={alertsHigh > 0 ? 'destructive' : 'neutral'}
            onClick={() => navigate('/alerts-incidents?only_alerts=1&severity=1')}
          />
        )}
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <Card>
          <CardHeader>
            <ChartArea size={16} className="text-sky-300" />
            <CardTitle>Recordings over time</CardTitle>
          </CardHeader>
          <CardContent>
            {loadingRecs ? (
              <Skeleton className="h-56" />
            ) : (
              <div className="h-56">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={recordingsByDay} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
                    <defs>
                      <linearGradient id="recGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#38bdf8" stopOpacity={0.5} />
                        <stop offset="95%" stopColor="#38bdf8" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.15)" />
                    <XAxis dataKey="day" stroke="var(--text-dim)" fontSize={12} />
                    <YAxis stroke="var(--text-dim)" fontSize={12} allowDecimals={false} />
                    <RTooltip contentStyle={{ background: 'var(--panel-2)', border: '1px solid rgb(64,64,64)', color: 'var(--text)' }} />
                    <Area type="monotone" dataKey="count" stroke="#38bdf8" fill="url(#recGrad)" strokeWidth={2} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <ChartBar size={16} className="text-emerald-300" />
            <CardTitle>Cameras by status</CardTitle>
          </CardHeader>
          <CardContent>
            {loadingCams ? (
              <Skeleton className="h-56" />
            ) : (
              <div className="h-56">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={camerasByStatus} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.15)" />
                    <XAxis dataKey="name" stroke="var(--text-dim)" fontSize={12} />
                    <YAxis stroke="var(--text-dim)" fontSize={12} allowDecimals={false} />
                    <RTooltip contentStyle={{ background: 'var(--panel-2)', border: '1px solid rgb(64,64,64)', color: 'var(--text)' }} />
                    <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                      {camerasByStatus.map((entry) => {
                        const color = entry.name === 'Online' ? '#60a5fa' : entry.name === 'Degraded' ? '#34d399' : '#ef4444'
                        return <Cell key={entry.name} fill={color} />
                      })}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Per-camera recording availability */}
      <Card>
        <CardHeader>
          <HardDrive size={16} className="text-[var(--text-dim)]" />
          <CardTitle>Recordings by camera</CardTitle>
        </CardHeader>
        <CardContent>
          {loadingRecs ? (
            <div className="space-y-2">
              {Array.from({ length: 4 }).map((_, i) => (
                <Skeleton key={i} className="h-9" />
              ))}
            </div>
          ) : recsErr ? (
            <ErrorCard title="Recordings" message={recsErr} onRetry={() => recsQuery.refetch()} />
          ) : recsByCamera.length === 0 ? (
            <div className="text-sm text-[var(--text-dim)]">No recordings yet</div>
          ) : (
            <div className="overflow-x-auto border border-neutral-700 rounded">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[var(--text-dim)] border-b border-neutral-700 bg-[var(--panel-2)]">
                    <th className="py-2 px-3 font-medium">Camera</th>
                    <th className="py-2 pr-4 font-medium">Recorded footage</th>
                    <th className="py-2 pr-4 font-medium">Days</th>
                    <th className="py-2 pr-4 font-medium">Latest</th>
                    <th className="py-2 pr-4" />
                  </tr>
                </thead>
                <tbody>
                  {recsByCamera.map((c) => (
                    <tr key={c.id} className="border-b border-neutral-800 last:border-b-0">
                      <td className="py-2 px-3 text-[var(--text)] font-medium">{c.name}</td>
                      <td className="py-2 pr-4 text-[var(--text)]">
                        {c.days > 0 ? (
                          formatDuration(c.duration)
                        ) : (
                          <span className="text-[var(--text-dim)]">No recordings</span>
                        )}
                      </td>
                      <td className="py-2 pr-4 text-[var(--text-dim)]">{c.days > 0 ? c.days : '—'}</td>
                      <td className="py-2 pr-4 text-[var(--text-dim)]">{c.latest ? fmtDay(c.latest) : '—'}</td>
                      <td className="py-2 pr-4 text-right">
                        {c.days > 0 && (
                          <Link
                            to="/playback"
                            className="inline-flex items-center gap-1 px-2 py-1 rounded bg-[var(--panel-2)] border border-neutral-700 hover:bg-[var(--panel)] text-xs"
                          >
                            <Play size={12} /> Browse
                          </Link>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Host resource health (CPU / RAM / recordings disk) */}
      <SystemHealthCard />

      {/* System & Network Monitoring (moved below Cameras by status) */}
      <SystemNetworkMonitoring />

      {/* Cameras grid */}
      {/* <Card>
        <CardHeader>
          <Camera size={16} className="text-[var(--text-dim)]" />
          <CardTitle>Live cameras</CardTitle>
          <div className="ml-auto text-xs text-[var(--text-dim)]">showing up to 9</div>
        </CardHeader>
        <CardContent>
          {loadingCams ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="aspect-video" />
              ))}
            </div>
          ) : camsErr ? (
            <ErrorCard title="Cameras" message={camsErr} onRetry={() => camsQuery.refetch()} />
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
              {(cams || []).slice(0, 9).map((c) => (
                <CameraTile key={c.id} cam={c} status={statusOf(c)} recording={c.recording_state === 'recording'} />
              ))}
            </div>
          )}
        </CardContent>
      </Card> */}

      {/* Recent recordings
      <Card>
        <CardHeader>
          <HardDrive size={16} className="text-[var(--text-dim)]" />
          <CardTitle>Recent recordings</CardTitle>
        </CardHeader>
        <CardContent>
          {loadingRecs ? (
            <div className="space-y-2">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-9" />
              ))}
            </div>
          ) : recsErr ? (
            <ErrorCard title="Recordings" message={recsErr} onRetry={fetchRecordings} />
          ) : (
            <div className="overflow-x-auto border border-neutral-700 rounded">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[var(--text-dim)] border-b border-neutral-700 bg-[var(--panel-2)]">
                    <th className="py-2 pr-4">Time</th>
                    <th className="py-2 pr-4">Camera</th>
                    <th className="py-2 pr-4">Size</th>
                    <th className="py-2 pr-4">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {(recs || []).slice(0, 10).map((r) => (
                    <tr key={r.id} className="border-b border-neutral-800">
                      <td className="py-2 pr-4 text-[var(--text)]">{r.start_time ? new Date(r.start_time).toLocaleString() : '—'}</td>
                      <td className="py-2 pr-4 text-[var(--text-dim)]">{r.camera || '—'}</td>
                      <td className="py-2 pr-4 text-[var(--text-dim)]">{r.size ? `${(r.size / (1024 * 1024)).toFixed(1)} MB` : '—'}</td>
                      <td className="py-2 pr-4">
                        {r.url ? (
                          <a className="inline-flex items-center gap-1 px-2 py-1 rounded bg-[var(--panel-2)] border border-neutral-700 hover:bg-[var(--panel)]" href={r.url} target="_blank" rel="noreferrer">
                            <Play size={12} /> Play
                          </a>
                        ) : (
                          <span className="text-[var(--text-dim)]">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card> */}


    </section>
  )
}

function formatBytesShort(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v >= 1024 ** 4) return `${(v / 1024 ** 4).toFixed(2)} TB`
  return `${(v / 1024 ** 3).toFixed(1)} GB`
}

/** Host CPU / RAM / recordings-disk tiles, fed by the 15s monitor snapshot. */
function SystemHealthCard() {
  const { data, isLoading, error, refetch } = useSystemResources()
  const thr = data?.thresholds
  const cpu = data?.cpu_percent
  const mem = data?.memory
  const disk = data?.disk

  const cpuThr = thr?.cpu_percent_threshold
  const memThr = thr?.memory_percent_threshold
  const diskThr = thr?.disk_used_percent_threshold

  return (
    <Card>
      <CardHeader>
        <HardDrive size={16} className="text-[var(--text-dim)]" />
        <CardTitle>System health</CardTitle>
        {(data?.active_alerts?.length ?? 0) > 0 && (
          <Badge variant="warning">{data!.active_alerts!.length} active alert{data!.active_alerts!.length > 1 ? 's' : ''}</Badge>
        )}
        <div className="ml-auto text-xs text-[var(--text-dim)]">host resources · 15s refresh</div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24" />
        ) : error ? (
          <ErrorCard title="System health" message={extractApiError(error, 'Failed to load system resources')} onRetry={() => refetch()} />
        ) : !data?.sampled_at ? (
          <div className="text-sm text-[var(--text-dim)]">First resource sample pending…</div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
            <StatTile
              label="CPU"
              value={cpu != null ? `${Math.round(cpu)}%` : '—'}
              sub={data.monitoring_available === false ? 'psutil not installed' : (data.load_avg ? `load ${data.load_avg.map(v => v.toFixed(1)).join(' / ')}` : undefined)}
              warn={cpu != null && cpuThr != null && cpu >= cpuThr}
            />
            <StatTile
              label="Memory"
              value={mem ? `${Math.round(mem.percent)}%` : '—'}
              sub={mem ? `${formatBytesShort(mem.used)} of ${formatBytesShort(mem.total)}` : undefined}
              warn={mem != null && memThr != null && mem.percent >= memThr}
            />
            <div className="border border-[var(--border)] rounded bg-[var(--bg-2)] p-3">
              <div className="text-[11px] uppercase tracking-wider text-[var(--text-dim)] font-mono">Recordings disk</div>
              {disk ? (
                <>
                  <div className={`font-mono text-lg font-bold tabular-nums mt-1 ${disk.percent >= 98 ? 'text-red-400' : diskThr != null && disk.percent >= diskThr ? 'text-amber-400' : 'text-[var(--text)]'}`}>
                    {formatBytesShort(disk.free)} free
                  </div>
                  <div className="text-[11px] text-[var(--text-dim)] mt-0.5 mb-1.5">
                    {formatBytesShort(disk.used)} of {formatBytesShort(disk.total)} used ({Math.round(disk.percent)}%)
                  </div>
                  <UsageBar
                    used={disk.used}
                    total={disk.total}
                    warnAt={diskThr != null ? diskThr / 100 : 0.8}
                    critAt={0.98}
                  />
                </>
              ) : (
                <div className="text-sm text-[var(--text-dim)] mt-1">{data.disk_error ? `unavailable: ${data.disk_error}` : '—'}</div>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
