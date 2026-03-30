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

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Camera, ChartArea, ChartBar, CircleCheck, CircleDashed, CircleAlert, RefreshCw, AlertTriangle, Activity, HardDrive, Play, Info } from 'lucide-react'
import { ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip as RTooltip, CartesianGrid, BarChart, Bar, Cell } from 'recharts'
import { apiService } from '../lib/apiService'
import SystemNetworkMonitoring from './SystemNetworkMonitoring'
import { isMediaMtxHealthy } from '../lib/mtxHealth'

// Lightweight UI primitives aligned with existing app patterns
export function Card({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <div className={`rounded border border-neutral-700 bg-[var(--panel-2)] ${className}`}>{children}</div>
}
export function CardHeader({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <div className={`px-4 py-3 border-b border-neutral-700 flex items-center gap-2 ${className}`}>{children}</div>
}
export function CardTitle({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <h3 className={`text-sm font-semibold text-[var(--text)] tracking-wide ${className}`}>{children}</h3>
}
export function CardContent({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <div className={`p-4 ${className}`}>{children}</div>
}
function Badge({ children, variant = 'neutral', className = '' }: { children: React.ReactNode; variant?: 'success' | 'warning' | 'destructive' | 'neutral' | 'info'; className?: string }) {
  const styles = {
    success: 'bg-green-900/50 text-green-400',
    warning: 'bg-yellow-900/50 text-yellow-400',
    destructive: 'bg-red-900/50 text-red-400',
    neutral: 'bg-gray-900/50 text-gray-400',
    info: 'bg-blue-900/50 text-blue-400',
  } as const
  return <span className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] ${styles[variant]} ${className}`}>{children}</span>
}
function Button({ children, onClick, className = '', disabled }: { children: React.ReactNode; onClick?: () => void; className?: string; disabled?: boolean }) {
  return (
    <button onClick={onClick} disabled={disabled} className={`inline-flex items-center gap-2 rounded border border-neutral-700 bg-[var(--panel-2)] px-3 py-1.5 text-sm hover:bg-[var(--panel)] disabled:opacity-50 ${className}`}>
      {children}
    </button>
  )
}
export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`animate-pulse rounded-md bg-[var(--bg-2)] ${className}`} />
}

type CameraItem = {
  id: number
  name: string
  ip_address: string
  is_active: boolean
  status?: string | null
}
type CameraListResp = { cameras: CameraItem[]; total: number }
type RecordingItem = { start_time?: string | null; id: number; camera?: string; relpath?: string; url?: string; size?: number }
type RecordingsResp = { items: RecordingItem[]; total: number }

type MediaMtxStatus = {
  camera_id: number
  camera_name?: string
  path_configured?: boolean
  path_active?: boolean
  path_status?: any
  active_path?: any
  recording_status?: { recording_enabled?: boolean }
}

function usePolling(enabled: boolean, intervalMs: number, fn: () => void) {
  useEffect(() => {
    if (!enabled) return
    fn()
    const id = setInterval(fn, intervalMs)
    return () => clearInterval(id)
  }, [enabled, intervalMs, fn])
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

function StatusDot({ status }: { status: 'online' | 'offline' | 'degraded' | 'error' }) {
  const map = { online: 'bg-emerald-500', offline: 'bg-slate-500', degraded: 'bg-amber-500', error: 'bg-red-500' } as const
  return <span className={`inline-block w-2 h-2 rounded-full ${map[status]}`} />
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

export function ErrorCard({ title = 'Error', message, onRetry }: { title?: string; message: string; onRetry?: () => void }) {
  return (
    <Card className="border-red-700/40">
      <CardHeader>
        <CircleAlert size={16} className="text-red-300" />
        <CardTitle>{title}</CardTitle>
        {onRetry && <div className="ml-auto"><Button onClick={onRetry}><RefreshCw size={14} /> Retry</Button></div>}
      </CardHeader>
      <CardContent>
        <div className="text-sm text-red-300/90">{message}</div>
      </CardContent>
    </Card>
  )
}

function toStringSafe(v: any): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  if (Array.isArray(v)) return v.map(toStringSafe).filter(Boolean).join(', ')
  if (typeof v === 'object' && typeof (v as any).msg === 'string') return (v as any).msg
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

function extractApiError(e: any, fallback: string): string {
  const detail = e?.data?.detail ?? e?.response?.data?.detail
  const msg = toStringSafe(detail) || (typeof e?.message === 'string' ? e.message : '')
  return msg || fallback
}

export function Dashboard() {
  const navigate = useNavigate()
  const [cams, setCams] = useState<CameraItem[] | null>(null)
  const [camsTotal, setCamsTotal] = useState<number>(0)
  const [camsErr, setCamsErr] = useState<string | null>(null)
  const [loadingCams, setLoadingCams] = useState<boolean>(true)

  const [recs, setRecs] = useState<RecordingItem[] | null>(null)
  const [recsTotal, setRecsTotal] = useState<number>(0)
  const [recsErr, setRecsErr] = useState<string | null>(null)
  const [loadingRecs, setLoadingRecs] = useState<boolean>(true)

  // Alerts (High severity)
  const [alertsHigh, setAlertsHigh] = useState<number>(0)
  const [alertsErr, setAlertsErr] = useState<string | null>(null)
  const [loadingAlerts, setLoadingAlerts] = useState<boolean>(true)

  const [polling, setPolling] = useState<boolean>(false)
  const [refreshing, setRefreshing] = useState<boolean>(false)

  // Per-camera live status (from media-server)
  const [liveStatuses, setLiveStatuses] = useState<Record<number, MediaMtxStatus>>({})

  const fetchCameras = useCallback(async () => {
    setLoadingCams(true)
    setCamsErr(null)
    try {
      const { data } = await apiService.getCameras({ limit: 100, active_only: true })
      const resp = data as CameraListResp
      setCams(resp.cameras || [])
      setCamsTotal(resp.total || (resp.cameras?.length || 0))
    } catch (e: any) {
      setCamsErr(extractApiError(e, 'Failed to load cameras'))
      setCams([])
      setCamsTotal(0)
    } finally {
      setLoadingCams(false)
    }
  }, [])

  const fetchRecordings = useCallback(async () => {
    setLoadingRecs(true)
    setRecsErr(null)
    try {
      const { data } = await apiService.getRecordingsByDate()
      // New format: { cameras: [{ recordings: [{ date, total_duration }] }], total_recordings }
      // Flatten all daily recordings from all cameras for chart
      const dailyRecs: RecordingItem[] = []
      for (const cam of data?.cameras || []) {
        for (const rec of cam.recordings || []) {
          dailyRecs.push({ id: 0, start_time: rec.date, camera: cam.camera_name })
        }
      }
      setRecs(dailyRecs)
      setRecsTotal(data?.total_recordings || 0)
    } catch (e: any) {
      setRecsErr(extractApiError(e, 'Failed to load recordings'))
      setRecs([])
      setRecsTotal(0)
    } finally {
      setLoadingRecs(false)
    }
  }, [])

  const fetchAlerts = useCallback(async () => {
    setLoadingAlerts(true)
    setAlertsErr(null)
    try {
      const { data } = await apiService.getSuricataStats({ limit: 5000 })
      const high = (data?.by_severity?.['1'] as number) || 0
      setAlertsHigh(high)
    } catch (e: any) {
      setAlertsErr(extractApiError(e, 'No alert endpoint configured'))
      setAlertsHigh(0)
    } finally {
      setLoadingAlerts(false)
    }
  }, [])

  const fetchLiveStatuses = useCallback(async (cameras: CameraItem[]) => {
    // Query all cameras for accurate counts
    const results: Record<number, MediaMtxStatus> = {}
    await Promise.all(
      cameras.map(async (c) => {
        try {
          const { data } = await apiService.getCameraMediaMTXStatus(c.id)
          results[c.id] = data as MediaMtxStatus
        } catch {
          results[c.id] = { camera_id: c.id }
        }
      })
    )
    setLiveStatuses((prev) => ({ ...prev, ...results }))
  }, [])

  const refreshAll = useCallback(async () => {
    setRefreshing(true)
    await Promise.all([fetchCameras(), fetchRecordings(), fetchAlerts()])
    setRefreshing(false)
  }, [fetchCameras, fetchRecordings, fetchAlerts])

  useEffect(() => {
    // Speed up first paint: fetch cameras first (cheap) then recordings
    fetchCameras();
    // Slightly defer recordings so UI renders fast
    const id = setTimeout(() => { fetchRecordings() }, 250)
    const id2 = setTimeout(() => { fetchAlerts() }, 350)
    return () => { clearTimeout(id); clearTimeout(id2) }
  }, [fetchCameras, fetchRecordings, fetchAlerts])

  useEffect(() => {
    if (cams && cams.length) {
      const id = setTimeout(async () => {
        const healthy = await isMediaMtxHealthy(15000)
        if (!healthy) return
        fetchLiveStatuses(cams)
      }, 500)
      return () => clearTimeout(id)
    }
  }, [cams, fetchLiveStatuses])

  usePolling(polling, 30000, () => { refreshAll() })

  const onlineCount = useMemo(() => {
    if (!cams) return 0
    let count = 0
    for (const c of cams) {
      const st = liveStatuses[c.id]
      // Online = active_path.details shows ready:true && bytesReceived > 0
      const details = st?.active_path?.details
      if (details?.ready === true && (details?.bytesReceived || 0) > 0) count++
    }
    return count
  }, [cams, liveStatuses])

  const statusOf = useCallback((c: CameraItem): 'online' | 'offline' | 'degraded' | 'error' => {
    // Check camera.status for error/failed first
    if (c.status && ['error', 'failed'].includes(c.status)) return 'error'

    const st = liveStatuses[c.id]
    const details = st?.active_path?.details

    // Online = active_path.details.ready && bytesReceived > 0 (real data flowing)
    if (details?.ready === true && (details?.bytesReceived || 0) > 0) return 'online'

    // Degraded = camera is provisioned/configured but no data flowing
    // path_configured means MediaMTX has a path, or camera status indicates it should be provisioned
    if (st?.path_configured || c.status === 'provisioned' || c.status === 'active') return 'degraded'

    // Offline = camera not provisioned or no path/config
    return 'offline'
  }, [liveStatuses])

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
          <Button onClick={refreshAll} disabled={refreshing}><RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} /> Refresh</Button>
          <Button onClick={() => setPolling((s) => !s)}>{polling ? <CircleCheck size={14} /> : <CircleDashed size={14} />} {polling ? 'Polling: On' : 'Polling: Off'}</Button>
        </div>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {loadingCams ? (
          <Skeleton className="h-24" />
        ) : camsErr ? (
          <ErrorCard title="Cameras" message={camsErr} onRetry={fetchCameras} />
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
          <ErrorCard title="Recordings" message={recsErr} onRetry={fetchRecordings} />
        ) : (
          <KpiCard
            icon={<HardDrive size={18} />}
            label="Recordings"
            value={recsTotal}
            help="Stored recordings (database)"
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
            <ErrorCard title="Cameras" message={camsErr} onRetry={fetchCameras} />
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
              {(cams || []).slice(0, 9).map((c) => (
                <CameraTile key={c.id} cam={c} status={statusOf(c)} recording={!!liveStatuses[c.id]?.recording_status?.recording_enabled} />
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
