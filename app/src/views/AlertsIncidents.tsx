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

import React, { useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useVirtualizer } from '@tanstack/react-virtual'
import { apiService } from '../lib/apiService'
import { useSystemAlerts, type SystemAlertEvent } from '../hooks/useCameraStatus'
import { useMutation, useQuery as useRQ, useQueryClient } from '@tanstack/react-query'
import { alertsInboxService, type InboxAlert } from '../services/alertsInboxService'

type EveAlert = any

function useQuery() {
  const { search } = useLocation()
  return useMemo(() => new URLSearchParams(search), [search])
}

export function AlertsIncidents() {
  const query = useQuery()
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [items, setItems] = useState<EveAlert[]>([])

  // 'network' (Suricata IDS, the original view — default so existing deep
  // links keep working) or 'system' (host/recording alerts).
  const raw = query.get('source')
  const source: 'network' | 'system' | 'apps' =
    raw === 'system' ? 'system' : raw === 'apps' ? 'apps' : 'network'
  const onlyAlerts = query.get('only_alerts') === '1' || query.get('only_alerts') === 'true'
  const severity = query.get('severity') // Suricata: 1=high, 2=med, 3=low
  const category = query.get('category')

  useEffect(() => {
    if (source !== 'network') return
    let cancelled = false
    async function load() {
      setLoading(true)
      setError(null)
      try {
        const { data } = await apiService.getSuricataEveLogs({ limit: 5000, only_alerts: true })
        const all = (data?.items || []) as EveAlert[]
        let filtered = all
        if (onlyAlerts) {
          filtered = filtered.filter((e: any) => e?.event_type === 'alert')
        }
        if (severity) {
          filtered = filtered.filter((e: any) => String(e?.alert?.severity) === String(severity))
        }
        if (category) {
          filtered = filtered.filter((e: any) => (e?.alert?.category || '').toLowerCase() === String(category).toLowerCase())
        }
        // sort by timestamp desc if present
        filtered = filtered.slice().sort((a: any, b: any) => {
          const ta = a?.timestamp ? Date.parse(String(a.timestamp).replace('+0000', '+00:00').replace('Z', '+00:00')) : 0
          const tb = b?.timestamp ? Date.parse(String(b.timestamp).replace('+0000', '+00:00').replace('Z', '+00:00')) : 0
          return tb - ta
        })
        if (!cancelled && filtered.length > 0) {
          setItems(filtered)
          setLoading(false)
          return
        }
        // Fallback to fast.log if eve alerts empty
        const fast = await apiService.getSuricataFastLogs({ limit: 5000 })
        const mapped = (fast?.data?.items || []).map((it: any) => ({
          timestamp: it?.timestamp || it?.ts || '',
          alert: {
            severity: it?.priority,
            category: it?.classification,
            signature: it?.signature,
          },
          src_ip: it?.src_ip,
          src_port: it?.src_port,
          dest_ip: it?.dst_ip,
          dest_port: it?.dst_port,
          event_type: 'alert',
        }))
        let f = mapped as any[]
        if (severity) f = f.filter((e: any) => String(e?.alert?.severity) === String(severity))
        if (category) f = f.filter((e: any) => (e?.alert?.category || '').toLowerCase() === String(category).toLowerCase())
        f = f.slice().sort((a: any, b: any) => Date.parse(a.timestamp || '0') - Date.parse(b.timestamp || '0')).reverse()
        if (!cancelled) setItems(f)
      } catch (e: any) {
        if (!cancelled) setError(e?.message || 'Failed to load alerts')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [source, onlyAlerts, severity, category])

  const setParam = (key: string, value: string | null) => {
    const p = new URLSearchParams(query as any)
    if (value === null) p.delete(key)
    else p.set(key, value)
    navigate({ search: p.toString() })
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h1 className="text-xl font-semibold">Alerts &amp; Incidents</h1>
        {loading && <span className="text-xs text-[var(--text-dim)]">Loading…</span>}
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>

      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-[var(--text-dim)]">Source:</span>
        <button
          className={`px-2 py-1 rounded border ${source === 'network' ? 'bg-[var(--panel-2)] border-[var(--border)]' : 'border-neutral-700'}`}
          onClick={() => setParam('source', null)}
        >
          Network (IDS)
        </button>
        <button
          className={`px-2 py-1 rounded border ${source === 'system' ? 'bg-[var(--panel-2)] border-[var(--border)]' : 'border-neutral-700'}`}
          onClick={() => setParam('source', 'system')}
        >
          System
        </button>
        <button
          className={`px-2 py-1 rounded border ${source === 'apps' ? 'bg-[var(--panel-2)] border-[var(--border)]' : 'border-neutral-700'}`}
          onClick={() => setParam('source', 'apps')}
        >
          Apps
        </button>
      </div>

      {source === 'apps' ? (
        <AppAlertsView />
      ) : source === 'system' ? (
        <SystemAlertsView />
      ) : (
      <>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-[var(--text-dim)]">Filters:</span>
        <button
          className={`px-2 py-1 rounded border ${onlyAlerts ? 'bg-[var(--panel-2)] border-[var(--border)]' : 'border-neutral-700'}`}
          onClick={() => setParam('only_alerts', onlyAlerts ? null : '1')}
        >
          Alerts only
        </button>
        <button
          className={`px-2 py-1 rounded border ${severity === '1' ? 'bg-[var(--panel-2)] border-[var(--border)]' : 'border-neutral-700'}`}
          onClick={() => setParam('severity', severity === '1' ? null : '1')}
        >
          High
        </button>
        <button
          className={`px-2 py-1 rounded border ${severity === '2' ? 'bg-[var(--panel-2)] border-[var(--border)]' : 'border-neutral-700'}`}
          onClick={() => setParam('severity', severity === '2' ? null : '2')}
        >
          Medium
        </button>
        <button
          className={`px-2 py-1 rounded border ${severity === '3' ? 'bg-[var(--panel-2)] border-[var(--border)]' : 'border-neutral-700'}`}
          onClick={() => setParam('severity', severity === '3' ? null : '3')}
        >
          Low
        </button>
        {category && (
          <button
            className="px-2 py-1 rounded border border-neutral-700"
            onClick={() => setParam('category', null)}
          >
            Category: {category} ×
          </button>
        )}
      </div>

      <div className="flex items-center justify-between text-xs text-[var(--text-dim)]">
        <span>Showing {items.length} alert{items.length === 1 ? '' : 's'}</span>
        {(severity || category) && <span>Active filters: {severity ? `severity=${severity}` : ''} {category ? `category=${category}` : ''}</span>}
      </div>

      <VirtualAlertTable items={items} loading={loading} onCategoryClick={(c) => setParam('category', c)} />
      </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- system view

type SystemEventRow = {
  id: string
  event_type: string
  event_state: string | null
  severity: string
  description: string | null
  camera_id: number | null
  camera_name: string | null
  occurred_at: string | null
}

const SEVERITY_LABEL: Record<string, { text: string; className: string }> = {
  critical: { text: 'Critical', className: 'text-red-400' },
  warning: { text: 'Warning', className: 'text-amber-400' },
  info: { text: 'Info', className: 'text-[var(--text-dim)]' },
}

function humanAlertType(t: string): string {
  return t.replace(/_/g, ' ')
}

/**
 * Host + recording alert history (disk pressure, purge outcomes, CPU/RAM,
 * recording stalls) from /system/events, with live rows prepended from the
 * shared events socket. Low-volume — a plain table, no virtualization.
 */
function SystemAlertsView() {
  const [rows, setRows] = useState<SystemEventRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { recentAlerts } = useSystemAlerts()
  // Live alerts that arrived after the history load (the load already
  // contains everything persisted before it).
  const loadedAtRef = useRef<string>(new Date().toISOString())

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    apiService.getSystemEvents({ limit: 500 })
      .then(({ data }) => {
        if (cancelled) return
        loadedAtRef.current = new Date().toISOString()
        setRows((data?.events || []) as SystemEventRow[])
        setError(null)
      })
      .catch((e: any) => { if (!cancelled) setError(e?.message || 'Failed to load system alerts') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  const liveRows: SystemEventRow[] = useMemo(() =>
    recentAlerts
      .filter((a: SystemAlertEvent) => a.received_at > loadedAtRef.current)
      .map((a: SystemAlertEvent) => ({
        id: a.id,
        event_type: a.alert_type,
        event_state: a.state,
        severity: a.state === 'inactive' ? 'info' : a.severity,
        description: a.description,
        camera_id: a.camera_id ?? null,
        camera_name: a.camera_name ?? null,
        occurred_at: a.received_at,
      })),
    [recentAlerts])

  const all = useMemo(() => [...liveRows, ...rows], [liveRows, rows])

  return (
    <div className="space-y-2">
      <div className="text-xs text-[var(--text-dim)]">
        {loading ? 'Loading…' : error ? <span className="text-red-400">{error}</span> : `Showing ${all.length} system alert${all.length === 1 ? '' : 's'}`}
      </div>
      <div className="overflow-auto border border-[var(--border)] bg-[var(--panel-2)] rounded max-h-[calc(100vh-19rem)] min-h-48">
        <table className="w-full text-xs min-w-[760px]">
          <thead>
            <tr className="text-left text-[var(--text-dim)] bg-[var(--bg-2)] border-b border-[var(--border)] sticky top-0 z-10">
              <th className="p-2 font-medium">Time</th>
              <th className="p-2 font-medium">Severity</th>
              <th className="p-2 font-medium">Type</th>
              <th className="p-2 font-medium">Message</th>
              <th className="p-2 font-medium">Camera</th>
            </tr>
          </thead>
          <tbody>
            {all.length === 0 && !loading ? (
              <tr><td colSpan={5} className="text-center text-[var(--text-dim)] py-6">No system alerts recorded.</td></tr>
            ) : all.map((r, i) => {
              const sev = SEVERITY_LABEL[r.severity] || SEVERITY_LABEL.info
              return (
                <tr key={r.id} className={i % 2 === 0 ? 'bg-[var(--bg-2)]' : 'bg-[var(--panel)]'}>
                  <td className="p-2 whitespace-nowrap">{r.occurred_at ? new Date(r.occurred_at).toLocaleString() : '—'}</td>
                  <td className={`p-2 ${sev.className}`}>{sev.text}</td>
                  <td className="p-2 whitespace-nowrap capitalize">
                    {humanAlertType(r.event_type)}
                    {r.event_state ? <span className="text-[var(--text-dim)]"> · {r.event_state === 'active' ? 'raised' : 'resolved'}</span> : null}
                  </td>
                  <td className="p-2">{r.description || '—'}</td>
                  <td className="p-2 whitespace-nowrap">{r.camera_name || (r.camera_id != null ? `Camera ${r.camera_id}` : '—')}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// Suricata can return thousands of rows; render only the visible window.
const GRID_COLS = 'grid grid-cols-[170px_90px_200px_minmax(240px,1fr)_160px_160px]'
const ROW_HEIGHT = 33

function VirtualAlertTable({ items, loading, onCategoryClick }: { items: EveAlert[]; loading: boolean; onCategoryClick: (category: string) => void }) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const rowVirtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  })

  return (
    <div ref={scrollRef} className="overflow-auto border border-[var(--border)] bg-[var(--panel-2)] rounded max-h-[calc(100vh-19rem)] min-h-48">
      <div className="min-w-[1020px] text-xs">
        <div className={`${GRID_COLS} sticky top-0 z-10 bg-[var(--bg-2)] text-[var(--text-dim)] border-b border-[var(--border)]`}>
          <div className="p-2 font-medium">Time</div>
          <div className="p-2 font-medium">Severity</div>
          <div className="p-2 font-medium">Category</div>
          <div className="p-2 font-medium">Signature</div>
          <div className="p-2 font-medium">Source</div>
          <div className="p-2 font-medium">Destination</div>
        </div>
        {items.length === 0 && !loading ? (
          <div className="text-center text-[var(--text-dim)] py-6">No alerts found for current filters.</div>
        ) : (
          <div style={{ height: rowVirtualizer.getTotalSize(), position: 'relative' }}>
            {rowVirtualizer.getVirtualItems().map((vRow) => {
              const e: any = items[vRow.index]
              return (
                <div
                  key={vRow.key}
                  className={`${GRID_COLS} ${vRow.index % 2 === 0 ? 'bg-[var(--bg-2)]' : 'bg-[var(--panel)]'}`}
                  style={{ position: 'absolute', top: 0, left: 0, right: 0, height: vRow.size, transform: `translateY(${vRow.start}px)` }}
                >
                  <div className="p-2 whitespace-nowrap truncate">{e?.timestamp ? new Date(String(e.timestamp).replace('+0000', '+00:00')).toLocaleString() : '-'}</div>
                  <div className="p-2">
                    {typeof e?.alert?.severity !== 'undefined' ? (
                      e.alert.severity === 1 ? 'High' : e.alert.severity === 2 ? 'Medium' : e.alert.severity === 3 ? 'Low' : String(e.alert.severity)
                    ) : '-'}
                  </div>
                  <div className="p-2 truncate" title={e?.alert?.category || ''}>
                    <button
                      className="underline hover:opacity-80"
                      onClick={() => e?.alert?.category && onCategoryClick(e.alert.category)}
                    >
                      {e?.alert?.category || '-'}
                    </button>
                  </div>
                  <div className="p-2 truncate" title={e?.alert?.signature || ''}>{e?.alert?.signature || '-'}</div>
                  <div className="p-2 truncate" title={`${e?.src_ip || ''}${e?.src_port ? ':' + e.src_port : ''}`}>{e?.src_ip}{e?.src_port ? `:${e.src_port}` : ''}</div>
                  <div className="p-2 truncate" title={`${e?.dest_ip || e?.dst_ip || ''}${e?.dest_port ? ':' + e.dest_port : (e?.dst_port ? ':' + e.dst_port : '')}`}>{e?.dest_ip || e?.dst_ip}{e?.dest_port ? `:${e.dest_port}` : (e?.dst_port ? `:${e.dst_port}` : '')}</div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

// ── App alerts (the operator inbox) ────────────────────────────────
//
// §11.5 app-emitted alerts landed by the core's opennvr.alerts.>
// consumer — the full history behind the top-bar bell (which shows only
// unacknowledged rows). Same table the bell acknowledges into, so an
// ack here silences every open browser too.

const APP_SEVERITY_STYLE: Record<string, string> = {
  critical: 'bg-red-600 text-white',
  high: 'bg-orange-600 text-white',
  medium: 'bg-yellow-600 text-black',
  low: 'bg-neutral-600 text-white',
}

function AppAlertsView() {
  const qc = useQueryClient()
  const [onlyUnacked, setOnlyUnacked] = useState(false)
  const list = useRQ({
    queryKey: ['alerts-inbox-page', onlyUnacked],
    queryFn: async () => {
      const { data } = await alertsInboxService.listInboxAlerts({
        unacked: onlyUnacked || undefined,
        limit: 200,
      })
      return data as { alerts: InboxAlert[]; unacked_count: number }
    },
    refetchInterval: 15_000,
  })
  const ack = useMutation({
    mutationFn: (ids?: number[]) => alertsInboxService.ackInboxAlerts(ids),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['alerts-inbox-page'] })
      qc.invalidateQueries({ queryKey: ['alerts-inbox-unacked'] })
    },
  })
  const rows = list.data?.alerts ?? []
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-sm">
        <button
          className={`px-2 py-1 rounded border ${onlyUnacked ? 'bg-[var(--panel-2)] border-[var(--border)]' : 'border-neutral-700'}`}
          onClick={() => setOnlyUnacked((s) => !s)}
        >
          Unacknowledged only
        </button>
        {(list.data?.unacked_count ?? 0) > 0 && (
          <button
            className="px-2 py-1 rounded border border-neutral-700 hover:bg-[var(--panel-2)]"
            onClick={() => ack.mutate(undefined)}
          >
            Acknowledge all ({list.data?.unacked_count})
          </button>
        )}
      </div>
      <div className="border border-[var(--border)] rounded overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-[var(--panel-2)] text-left">
            <tr>
              <th className="px-3 py-2">Severity</th>
              <th className="px-3 py-2">Alert</th>
              <th className="px-3 py-2">Source</th>
              <th className="px-3 py-2">Camera</th>
              <th className="px-3 py-2">Fired</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-[var(--text-dim)]">
                  {list.isLoading ? 'Loading…' : 'No app alerts yet'}
                </td>
              </tr>
            )}
            {rows.map((a) => (
              <tr key={a.id} className="border-t border-[var(--border)]">
                <td className="px-3 py-2">
                  <span className={`px-1.5 py-0.5 rounded text-[10px] uppercase ${APP_SEVERITY_STYLE[a.severity] ?? APP_SEVERITY_STYLE.low}`}>
                    {a.severity}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <div className="font-medium">{a.title}</div>
                  {a.description && (
                    <div className="text-[11px] text-[var(--text-dim)]">{a.description}</div>
                  )}
                </td>
                <td className="px-3 py-2 text-[var(--text-dim)]">{a.source_name || '—'}</td>
                <td className="px-3 py-2 text-[var(--text-dim)]">{a.camera_id || '—'}</td>
                <td className="px-3 py-2 text-[var(--text-dim)]">
                  {a.fired_at ? new Date(a.fired_at).toLocaleString() : '—'}
                </td>
                <td className="px-3 py-2">
                  {a.acknowledged_at ? (
                    <span className="text-[var(--text-dim)]">acked</span>
                  ) : (
                    <span className="text-red-400">unacked</span>
                  )}
                </td>
                <td className="px-3 py-2 text-right">
                  {!a.acknowledged_at && (
                    <button
                      className="px-2 py-0.5 rounded border border-neutral-700 hover:bg-[var(--panel-2)]"
                      onClick={() => ack.mutate([a.id])}
                    >
                      Ack
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
