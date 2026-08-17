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

// Read-only registry of AI adapters known to KAI-C: liveness, advertised
// tasks, model identity, and requested permissions (AI Adapter Contract v1).
// Registration, permission approval, and metrics come later with the KAI-C
// /api/v1/adapters migration.

import { useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Cpu, Database, Globe, HardDrive, Info, Layers, Lock, RefreshCw, ShieldAlert, ShieldCheck, Share2, Server } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { useSnackbar } from '../components/Snackbar'
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, EmptyState, ErrorCard, PageHeader, Skeleton, type BadgeVariant } from '../components/ui'
import { MetricPanel, Sparkline, SparkRow, StatTile } from '../components/ui/stats'

type AdapterInfo = Record<string, any>

type HealthResp = { kai_c_status?: string; adapters?: Record<string, AdapterInfo>; message?: string | null }
type CapabilitiesResp = { kai_c?: Record<string, any>; adapters?: Record<string, AdapterInfo> }

function useKaiHealth() {
  return useQuery({
    queryKey: ['kai-c-health'],
    queryFn: async () => {
      const { data } = await apiService.checkKAIHealth()
      return data as HealthResp
    },
    retry: 0,
  })
}

function useKaiCapabilities() {
  return useQuery({
    queryKey: ['kai-c-capabilities'],
    queryFn: async () => {
      const { data } = await apiService.getCapabilities()
      return data as CapabilitiesResp
    },
    retry: 0,
  })
}

function statusVariant(status?: string): BadgeVariant {
  switch ((status || '').toLowerCase()) {
    case 'ok':
    case 'healthy':
    case 'online':
      return 'success'
    case 'degraded':
    case 'loading':
      return 'warning'
    case 'error':
    case 'unhealthy':
    case 'offline':
      return 'destructive'
    default:
      return 'neutral'
  }
}

function asStringList(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(String)
  return []
}

/** Pull the interesting contract fields out of whatever shape the adapter reported. */
function summarizeAdapter(name: string, caps: AdapterInfo | undefined, health: AdapterInfo | undefined) {
  const status = (health?.status ?? health?.health ?? (typeof health === 'string' ? health : undefined)) as string | undefined
  const model = caps?.model ?? {}
  const tasks = asStringList(caps?.tasks_advertised).concat(asStringList(caps?.tasks))
  const permissions = caps?.permissions ?? {}
  const requestedPerms: string[] = []
  if (permissions.gpu) requestedPerms.push('GPU')
  for (const host of asStringList(permissions.network_egress)) requestedPerms.push(`egress: ${host}`)
  for (const path of asStringList(permissions.host_filesystem)) requestedPerms.push(`fs: ${path}`)
  for (const path of asStringList(permissions.shared_memory_paths)) requestedPerms.push(`shm: ${path}`)
  if (permissions.host_metadata) requestedPerms.push('host metadata')
  return {
    name,
    status,
    modelName: model.name ?? caps?.adapter?.name,
    modelVersion: model.version,
    framework: model.framework,
    fingerprint: typeof model.fingerprint === 'string' ? model.fingerprint : undefined,
    tasks: Array.from(new Set(tasks)),
    requestedPerms,
    raw: caps ?? health ?? {},
  }
}

/* ------------------------- Adapter metrics ------------------------- */
// Decision-grade metrics panel (design spec: capabilities-observability §06).
// Each panel is captioned with the operator decision it drives, per the
// "metrics grouped by the decision they drive" table.

type SeriesPoint = {
  ts: number
  inflight: number | null
  queue_depth: number | null
  rpm: number | null
  p95_ms: number | null
  cpu_percent: number | null
  memory_bytes: number | null
  gpu_utilization: number | null
  gpu_memory_bytes: number | null
}

type AdapterMetricsResp = {
  adapter: string
  window_s: number
  latency_ms: { p50: number | null; p95: number | null; p99: number | null }
  outcomes: Record<string, number>
  inflight: number | null
  max_inflight: number | null
  queue_depth: number | null
  hardware?: {
    cpu_percent: number | null
    memory_bytes: number | null
    gpu_utilization: number | null
    gpu_memory_bytes: number | null
  }
  series?: SeriesPoint[]
  fingerprint_changes: string[]
  samples: number
}

type FleetMetricsResp = {
  adapters: Record<string, AdapterMetricsResp & { status?: string }>
}

function formatBytes(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v >= 1 << 30) return `${(v / (1 << 30)).toFixed(1)} GiB`
  if (v >= 1 << 20) return `${Math.round(v / (1 << 20))} MiB`
  return `${Math.round(v / 1024)} KiB`
}

function formatMs(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ms`
}

function formatWindow(seconds: number | undefined): string {
  if (!seconds || seconds <= 0) return ''
  if (seconds % 3600 === 0) return `last ${seconds / 3600}h`
  if (seconds % 60 === 0) return `last ${seconds / 60}m`
  return `last ${seconds}s`
}

// ok is green; model errors are the model's fault (amber, tune/rollback);
// provider/transport/refused mean the serving path is broken (red).
function outcomeBarClass(outcome: string): string {
  if (outcome === 'ok') return 'bg-emerald-500'
  if (outcome === 'model_error') return 'bg-amber-500'
  return 'bg-red-500'
}

function LatencyBars({ latency }: { latency: AdapterMetricsResp['latency_ms'] }) {
  const scale = latency?.p99 ?? 0
  const rows = [
    { label: 'p50', value: latency?.p50 ?? null },
    { label: 'p95', value: latency?.p95 ?? null },
    { label: 'p99', value: latency?.p99 ?? null },
  ]
  return (
    <div className="space-y-1.5">
      {rows.map((r) => {
        const width = r.value != null && scale > 0 ? Math.min(100, Math.max(2, (r.value / scale) * 100)) : 0
        return (
          <div key={r.label} className="grid grid-cols-[32px_1fr_56px] items-center gap-2">
            <span className="font-mono text-xs text-[var(--text-dim)]">{r.label}</span>
            <div className="h-2 rounded bg-[var(--panel-2)] overflow-hidden">
              <div
                className={`h-full rounded ${r.label === 'p99' ? 'bg-amber-500' : 'bg-[var(--accent)]'}`}
                style={{ width: `${width}%` }}
              />
            </div>
            <span className="font-mono text-xs text-right tabular-nums text-[var(--text)]">{formatMs(r.value)}</span>
          </div>
        )
      })}
    </div>
  )
}

function OutcomesSplit({ outcomes }: { outcomes: Record<string, number> }) {
  const entries = Object.entries(outcomes ?? {}).filter(([, n]) => n > 0)
  const total = entries.reduce((sum, [, n]) => sum + n, 0)
  if (total === 0) return <div className="text-xs text-[var(--text-dim)]">No outcomes recorded in this window.</div>
  // ok first, then errors — stable, matches the legend order.
  entries.sort(([a], [b]) => (a === 'ok' ? -1 : b === 'ok' ? 1 : a.localeCompare(b)))
  return (
    <div>
      <div className="flex h-2.5 rounded overflow-hidden mb-2">
        {entries.map(([outcome, n]) => (
          <div key={outcome} className={outcomeBarClass(outcome)} style={{ width: `${(n / total) * 100}%` }} title={`${outcome}: ${n}`} />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {entries.map(([outcome, n]) => (
          <span key={outcome} className="inline-flex items-center gap-1.5 font-mono text-xs text-[var(--text-dim)]">
            <i className={`w-2 h-2 rounded-sm ${outcomeBarClass(outcome)}`} />
            {outcome} {((n / total) * 100).toFixed(total < 200 ? 0 : 1)}%
          </span>
        ))}
      </div>
    </div>
  )
}

function SaturationGauge({ inflight, maxInflight }: { inflight: number; maxInflight: number }) {
  const pct = maxInflight > 0 ? Math.round((inflight / maxInflight) * 100) : null
  const warn = pct != null && pct >= 80
  return (
    <div>
      <div className="flex items-baseline gap-2">
        <span className={`font-mono text-lg font-bold tabular-nums ${warn ? 'text-amber-400' : 'text-[var(--text)]'}`}>
          {inflight} / {maxInflight > 0 ? maxInflight : '—'}
        </span>
        <span className="font-mono text-xs text-[var(--text-dim)]">
          {pct != null ? `${pct}%${warn ? ' — near ceiling' : ''}` : 'no declared ceiling'}
        </span>
      </div>
      <div className="h-2 rounded bg-[var(--panel-2)] overflow-hidden mt-2">
        <div className={`h-full ${warn ? 'bg-amber-500' : 'bg-emerald-500'}`} style={{ width: `${Math.min(100, pct ?? 0)}%` }} />
      </div>
    </div>
  )
}

function FingerprintChanges({ changes }: { changes: string[] }) {
  const count = changes?.length ?? 0
  if (count === 0) return <div className="text-xs text-[var(--text-dim)]">No weight changes observed — fingerprint stable.</div>
  const latest = changes.reduce((a, b) => (a > b ? a : b))
  const latestDate = new Date(latest)
  return (
    <div className="flex items-baseline gap-2">
      <span className="font-mono text-lg font-bold tabular-nums text-[var(--text)]">{count}</span>
      <span className="font-mono text-xs text-[var(--text-dim)]">
        change{count === 1 ? '' : 's'} · latest {Number.isNaN(latestDate.getTime()) ? latest : latestDate.toLocaleString()}
      </span>
    </div>
  )
}

/* ----------------------- Permission approval ---------------------- */
// The visible proof of the governance story (design spec §06): an adapter
// declares the host resources it wants; nothing is granted until an operator
// approves it, and the gateway fails closed until then.

type PermissionKind = 'gpu' | 'network_egress' | 'host_filesystem' | 'shared_memory' | 'host_metadata'

type DeclaredPermission = {
  key: string
  label: string
  kind: PermissionKind
  sovereignty_conflict: boolean
}

type AdapterPermissionsResp = {
  adapter: string
  approval_status: 'pending' | 'approved'
  declared: DeclaredPermission[]
  granted: string[]
  pending: string[]
}

const PERMISSION_KIND_ICON: Record<PermissionKind, ReactNode> = {
  gpu: <Cpu size={13} />,
  network_egress: <Globe size={13} />,
  host_filesystem: <HardDrive size={13} />,
  shared_memory: <Share2 size={13} />,
  host_metadata: <Database size={13} />,
}

function permissionKindIcon(kind: PermissionKind): ReactNode {
  return PERMISSION_KIND_ICON[kind] ?? <Lock size={13} />
}

function useAdapterPermissions(name: string) {
  // The approval badge is important enough to fetch on mount for every card —
  // it's the governance status the operator needs to see immediately.
  return useQuery({
    queryKey: ['adapter-permissions', name],
    queryFn: async () => {
      const { data } = await apiService.getAdapterPermissions(name)
      return data as AdapterPermissionsResp
    },
    retry: 0,
  })
}

function AdapterApprovalBadge({ name }: { name: string }) {
  const query = useAdapterPermissions(name)
  if (query.isPending) return <Skeleton className="h-5 w-28" />
  // Fail loud, not silent: if we can't read approval status, never imply the
  // adapter is fine — surface an explicit unknown state so a pending adapter
  // can't hide behind a failed request.
  if (query.isError) {
    return (
      <Badge variant="neutral">
        <ShieldAlert size={12} /> approval status unavailable
      </Badge>
    )
  }
  const status = query.data?.approval_status
  if (status === 'approved') {
    return (
      <Badge variant="success">
        <ShieldCheck size={12} /> approved
      </Badge>
    )
  }
  if (status === 'pending') {
    return (
      <Badge variant="warning">
        <ShieldAlert size={12} /> approval required
      </Badge>
    )
  }
  return null
}

function AdapterPermissionsSection({ name }: { name: string }) {
  const [open, setOpen] = useState(false)
  const queryClient = useQueryClient()
  const { showSuccess, showError } = useSnackbar()
  const query = useAdapterPermissions(name)

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['adapter-permissions', name] })
    queryClient.invalidateQueries({ queryKey: ['kai-c-capabilities'] })
  }

  const grant = useMutation({
    mutationFn: (keys: string[]) => apiService.grantAdapterPermissions(name, keys),
    onSuccess: (_data, keys) => {
      invalidate()
      showSuccess(`Granted ${keys.length === 1 ? keys[0] : `${keys.length} permissions`} to ${name}.`)
    },
    onError: (err) => showError(extractApiError(err, 'Could not grant permission.')),
  })

  const revoke = useMutation({
    mutationFn: (keys: string[]) => apiService.revokeAdapterPermissions(name, keys),
    onSuccess: (_data, keys) => {
      invalidate()
      showSuccess(`Revoked ${keys.length === 1 ? keys[0] : `${keys.length} permissions`} from ${name}.`)
    },
    onError: (err) => showError(extractApiError(err, 'Could not revoke permission.')),
  })

  const approveAll = useMutation({
    mutationFn: () => apiService.approveAllAdapterPermissions(name),
    onSuccess: () => {
      invalidate()
      showSuccess(`Approved all permissions for ${name}.`)
    },
    onError: (err) => showError(extractApiError(err, 'Could not approve permissions.')),
  })

  const busy = grant.isPending || revoke.isPending || approveAll.isPending
  const p = query.data
  const granted = new Set(p?.granted ?? [])
  const pending = new Set(p?.pending ?? [])
  const anyPending = (p?.pending?.length ?? 0) > 0

  return (
    <div>
      <Button variant="ghost" className="text-xs px-2 py-1" onClick={() => setOpen(!open)}>
        <ShieldCheck size={12} /> {open ? 'Hide permissions' : 'Permissions'}
      </Button>
      {open && (
        <div className="mt-2">
          {query.isPending ? (
            <div className="space-y-2">
              {Array.from({ length: 3 }).map((_, i) => (
                <Skeleton key={i} className="h-10" />
              ))}
            </div>
          ) : query.isError ? (
            <div className="text-sm text-red-300/90 border border-red-700/40 rounded p-3">
              {extractApiError(query.error, 'Could not load adapter permissions.')}
            </div>
          ) : p ? (
            <div className="space-y-2">
              {p.approval_status === 'pending' && (
                <div className="flex items-start gap-2 text-xs text-amber-300 border border-amber-700/40 bg-amber-900/20 rounded p-3">
                  <ShieldAlert size={14} className="flex-shrink-0 mt-0.5" />
                  <span>This adapter cannot serve inference until its permissions are approved.</span>
                </div>
              )}

              {p.declared.length === 0 ? (
                <div className="text-xs text-[var(--text-dim)] border border-[var(--border)] rounded bg-[var(--bg-2)] p-3">
                  This adapter declares no host permissions — nothing to approve.
                </div>
              ) : (
                <div className="space-y-1.5">
                  {p.declared.map((perm) => {
                    const isGranted = granted.has(perm.key)
                    const isPending = pending.has(perm.key)
                    return (
                      <div
                        key={perm.key}
                        className="flex items-center gap-2 border border-[var(--border)] rounded bg-[var(--bg-2)] px-3 py-2"
                      >
                        <span className="text-[var(--text-dim)]">{permissionKindIcon(perm.kind)}</span>
                        <div className="min-w-0 flex-1">
                          <div className="text-sm truncate" title={perm.label}>{perm.label}</div>
                          {perm.sovereignty_conflict && (
                            <div className="flex items-center gap-1 text-[11px] text-red-400 mt-0.5">
                              <ShieldAlert size={11} /> conflicts with local_only
                            </div>
                          )}
                        </div>
                        <Badge variant={isGranted ? 'success' : 'warning'}>{isGranted ? 'granted' : 'pending'}</Badge>
                        {isGranted ? (
                          <Button
                            variant="danger"
                            className="text-xs px-2 py-1"
                            disabled={busy}
                            onClick={() => revoke.mutate([perm.key])}
                          >
                            Revoke
                          </Button>
                        ) : (
                          <Button
                            variant="primary"
                            className="text-xs px-2 py-1"
                            disabled={busy || !isPending}
                            onClick={() => grant.mutate([perm.key])}
                          >
                            Grant
                          </Button>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}

              {anyPending && (
                <div className="flex items-center justify-between gap-2 pt-1">
                  <div className="flex items-center gap-1 text-[11px] text-[var(--text-dim)]">
                    <Info size={11} /> {p.pending.length} permission{p.pending.length === 1 ? '' : 's'} awaiting approval
                  </div>
                  <Button variant="primary" className="text-xs px-2 py-1" disabled={busy} onClick={() => approveAll.mutate()}>
                    <ShieldCheck size={12} /> Approve all
                  </Button>
                </div>
              )}
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}

function AdapterMetricsSection({ name }: { name: string }) {
  const [open, setOpen] = useState(false)
  const query = useQuery({
    queryKey: ['adapter-metrics', name],
    queryFn: async () => {
      const { data } = await apiService.getAdapterMetrics(name)
      return data as AdapterMetricsResp
    },
    enabled: open, // lazy: only fetch once the operator opens the panel
    retry: 0,
  })

  const m = query.data
  const noSamples = m != null && (m.samples ?? 0) === 0

  return (
    <div>
      <div className="flex items-center gap-1">
        <Button variant="ghost" className="text-xs px-2 py-1" onClick={() => setOpen(!open)}>
          <Activity size={12} /> {open ? 'Hide metrics' : 'Metrics'}
        </Button>
        {open && !query.isPending && (
          <Button variant="ghost" className="text-xs px-2 py-1" onClick={() => query.refetch()} disabled={query.isFetching}>
            <RefreshCw size={12} className={query.isFetching ? 'animate-spin' : ''} />
          </Button>
        )}
      </div>
      {open && (
        <div className="mt-2">
          {query.isPending ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {Array.from({ length: 4 }).map((_, i) => (
                <Skeleton key={i} className="h-24" />
              ))}
            </div>
          ) : query.isError ? (
            <div className="text-sm text-red-300/90 border border-red-700/40 rounded p-3">
              {extractApiError(query.error, 'Could not load adapter metrics.')}
            </div>
          ) : noSamples ? (
            <div className="text-xs text-[var(--text-dim)] border border-[var(--border)] rounded bg-[var(--bg-2)] p-3">
              No samples yet — this adapter hasn't served governed inference in the {formatWindow(m?.window_s) || 'current'} window.
            </div>
          ) : m ? (
            <div className="space-y-2">
              <div className="font-mono text-[11px] text-[var(--text-dim)]">
                {formatWindow(m.window_s)}{m.samples != null ? ` · ${m.samples} samples` : ''}
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <MetricPanel title="Inference latency" decision="which model per camera; is the SLA breached">
                  <LatencyBars latency={m.latency_ms} />
                </MetricPanel>
                <MetricPanel title="Outcomes" decision="rollback / retire / investigate the adapter">
                  <OutcomesSplit outcomes={m.outcomes} />
                </MetricPanel>
                <MetricPanel title="Saturation" decision="scale out a replica / rebalance cameras">
                  <SaturationGauge inflight={m.inflight ?? 0} maxInflight={m.max_inflight ?? 0} />
                </MetricPanel>
                <MetricPanel title="Queue depth" decision="throttle fan-in / drop to keyframes">
                  <div className="flex items-baseline gap-2">
                    <span className={`font-mono text-lg font-bold tabular-nums ${(m.queue_depth ?? 0) > 0 ? 'text-amber-400' : 'text-[var(--text)]'}`}>
                      {m.queue_depth ?? 0}
                    </span>
                    <span className="font-mono text-xs text-[var(--text-dim)]">frames waiting on the model</span>
                  </div>
                </MetricPanel>
                <MetricPanel title="Fingerprint / drift" decision="re-validate accuracy; freeze for compliance">
                  <FingerprintChanges changes={m.fingerprint_changes ?? []} />
                </MetricPanel>
                {(m.series?.length ?? 0) > 1 && (
                  <MetricPanel title="Trends — last hour" decision="is it degrading or recovering; when did it change">
                    <div className="space-y-1.5">
                      <SparkRow label="p95" points={(m.series ?? []).map(pt => pt.p95_ms)}
                        latest={formatMs(m.latency_ms?.p95)} />
                      <SparkRow label="req/min" points={(m.series ?? []).map(pt => pt.rpm)}
                        latest={String((m.series ?? []).filter(pt => pt.rpm != null).slice(-1)[0]?.rpm ?? '—')} />
                      <SparkRow label="inflight" points={(m.series ?? []).map(pt => pt.inflight)}
                        latest={`${m.inflight ?? '—'}${m.max_inflight ? `/${m.max_inflight}` : ''}`} />
                    </div>
                  </MetricPanel>
                )}
                {m.hardware && (m.hardware.cpu_percent != null || m.hardware.memory_bytes != null
                  || m.hardware.gpu_utilization != null || m.hardware.gpu_memory_bytes != null) ? (
                  <MetricPanel title="Hardware" decision="smaller model / bigger card / move the adapter">
                    <div className="grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-xs">
                      <span className="text-[var(--text-dim)]">CPU</span>
                      <span className="tabular-nums">{m.hardware.cpu_percent != null ? `${Math.round(m.hardware.cpu_percent)}%` : '—'}</span>
                      <span className="text-[var(--text-dim)]">Memory</span>
                      <span className="tabular-nums">{formatBytes(m.hardware.memory_bytes)}</span>
                      <span className="text-[var(--text-dim)]">GPU util</span>
                      <span className="tabular-nums">{m.hardware.gpu_utilization != null ? `${Math.round(m.hardware.gpu_utilization)}%` : '—'}</span>
                      <span className="text-[var(--text-dim)]">GPU mem</span>
                      <span className="tabular-nums">{formatBytes(m.hardware.gpu_memory_bytes)}</span>
                    </div>
                    {(m.series?.length ?? 0) > 1 && (m.series ?? []).some(pt => pt.cpu_percent != null) && (
                      <div className="mt-1.5">
                        <SparkRow label="cpu %" points={(m.series ?? []).map(pt => pt.cpu_percent)}
                          latest={m.hardware.cpu_percent != null ? `${Math.round(m.hardware.cpu_percent)}%` : '—'} />
                      </div>
                    )}
                  </MetricPanel>
                ) : (
                  <MetricPanel title="Hardware" decision="smaller model / bigger card / move the adapter">
                    <div className="text-[11px] text-[var(--text-dim)]">
                      Not exported by this adapter — it MAY export adapter_process_cpu_percent /
                      adapter_process_memory_bytes / adapter_gpu_* gauges (optional, spec §05).
                    </div>
                  </MetricPanel>
                )}
              </div>
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}

function useFleetMetrics() {
  return useQuery({
    queryKey: ['adapter-fleet-metrics'],
    queryFn: async () => {
      const { data } = await apiService.getFleetMetrics()
      return data as FleetMetricsResp
    },
    retry: 0,
    refetchInterval: 60_000, // rides KAI-C's own 60s scrape cadence
  })
}

// The 5-second fleet answer before any scrolling: how many healthy, the
// worst p95, total request rate, and whether any model drifted.
function FleetStrip({ fleet }: { fleet: FleetMetricsResp | undefined }) {
  if (!fleet) return null
  const entries = Object.values(fleet.adapters ?? {})
  const withSamples = entries.filter((m) => (m.samples ?? 0) > 0)
  if (!entries.length) return null
  const ok = entries.filter((m) => (m.status ?? 'ok') === 'ok').length
  const worst = withSamples.reduce<{ name: string; p95: number } | null>((acc, m) => {
    const p95 = m.latency_ms?.p95
    if (p95 == null) return acc
    return !acc || p95 > acc.p95 ? { name: m.adapter, p95 } : acc
  }, null)
  const rpm = withSamples.reduce((sum, m) => {
    const last = (m.series ?? []).filter((pt) => pt.rpm != null).slice(-1)[0]
    return sum + (last?.rpm ?? 0)
  }, 0)
  const drifted = entries.filter((m) => (m.fingerprint_changes?.length ?? 0) > 0).length
  const nearCeiling = entries.filter((m) =>
    m.inflight != null && m.max_inflight ? m.inflight / m.max_inflight >= 0.8 : false).length
  return (
    <Card>
      <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-2 py-3 font-mono text-xs">
        <span><span className="text-emerald-400 font-bold tabular-nums">{ok}</span>
          <span className="text-[var(--text-dim)]">/{entries.length} adapters ok</span></span>
        <span><span className="text-[var(--text-dim)]">worst p95 </span>
          <span className="font-bold tabular-nums">{worst ? `${formatMs(worst.p95)}` : '—'}</span>
          {worst && <span className="text-[var(--text-dim)]"> ({worst.name})</span>}</span>
        <span><span className="font-bold tabular-nums">{Math.round(rpm)}</span>
          <span className="text-[var(--text-dim)]"> req/min fleet-wide</span></span>
        <span className={nearCeiling ? 'text-amber-400' : ''}>
          <span className="font-bold tabular-nums">{nearCeiling}</span>
          <span className={nearCeiling ? '' : 'text-[var(--text-dim)]'}> near capacity</span></span>
        <span className={drifted ? 'text-amber-400' : ''}>
          <span className="font-bold tabular-nums">{drifted}</span>
          <span className={drifted ? '' : 'text-[var(--text-dim)]'}> with model drift (1h)</span></span>
      </CardContent>
    </Card>
  )
}

/* --------------------- Compute-gated (Tier-0) --------------------- */
// The detect-pipeline's compute-gated inference, surfaced beside the AI
// adapters: process CPU/RAM (same shape the adapters export), how well the
// motion gate is skipping idle frames, escalate-vs-suppress, and the
// expensive-model (Tier-1) call count/latency. Read-only; scrapes the
// pipeline's :9109/metrics via the backend on the same 60s cadence.

type Tier0MetricsResp = {
  available: boolean
  reason?: 'disabled' | 'unreachable'
  mode?: 'not_running' | 'off' | 'shadow' | 'enforce'
  model?: string | null
  health?: {
    workers_up: number
    workers_total: number
    min_fps_ratio: number | null
    worst_camera: string | null
    restarts_total: number
  }
  detector?: {
    latency_avg_ms: number | null
    latency_p95_ms: number | null
    detections_total: number
    detections_by_class: Record<string, number>
    stage_latency_ms: Record<string, number>
  }
  process?: { cpu_percent: number | null; memory_bytes: number | null }
  frames?: {
    total: number
    detector_runs: number
    skipped_no_motion: number
    skipped_calibrating: number
    motion_gate_ratio: number | null
  }
  gate?: { escalations: number; suppressions: number; shadow_would_suppress: number }
  tier1?: {
    dispatched: number
    errors: number
    dropped: number
    inflight: number
    latency_avg_ms: number | null
    latency_p95_ms: number | null
  }
  promotion?: {
    override: 'off' | 'shadow' | 'enforce' | null
    shadow_since: string | null
    shadow_days: number | null
    would_save_ratio: number | null
    ready: boolean
  }
}

function useTier0Metrics() {
  return useQuery({
    queryKey: ['tier0-metrics'],
    queryFn: async () => {
      const { data } = await apiService.getTier0Metrics()
      return data as Tier0MetricsResp
    },
    retry: 0,
    refetchInterval: 60_000, // rides the pipeline's own scrape cadence
  })
}

const TIER0_MODE_LABEL: Record<NonNullable<Tier0MetricsResp['mode']>, { text: string; variant: BadgeVariant }> = {
  not_running: { text: 'not running', variant: 'neutral' },
  off: { text: 'gate off', variant: 'neutral' },
  shadow: { text: 'shadow (measuring)', variant: 'info' },
  enforce: { text: 'enforce', variant: 'success' },
}

function formatPct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return `${Math.round(v * 100)}%`
}

/**
 * Guided promotion — shadow's evidence becomes a one-click decision.
 *
 * Three states: collecting (shadow, < 7 days — doubles as the in-product
 * explainer of what Tier-0 is doing), ready (evidence + Enable button),
 * enforcing (live savings + always-visible revert). The server owns the
 * thresholds (promotion_evidence); this renders them. Enforcement is a
 * product decision, so Enable talks to a superuser-gated endpoint — other
 * users see the evidence without the button doing anything for them.
 */
function PromotionCard({ d }: { d: Tier0MetricsResp }) {
  const { showSuccess, showError } = useSnackbar()
  const queryClient = useQueryClient()
  const promo = d.promotion
  const setGate = useMutation({
    mutationFn: async (mode: 'shadow' | 'enforce') => {
      await apiService.setTier0Gate(mode)
      return mode
    },
    onSuccess: (mode) => {
      showSuccess(
        mode === 'enforce'
          ? 'Enforcement enabled — the pipeline applies it within ~30s'
          : 'Reverted to shadow (measure-only)'
      )
      queryClient.invalidateQueries({ queryKey: ['tier0-metrics'] })
    },
    onError: (e) => showError(extractApiError(e, 'Failed to change gate mode')),
  })

  if (!promo) return null

  // enforcing (by override or by env) — show live savings + the way back
  if (d.mode === 'enforce') {
    const g = d.gate
    return (
      <div className="border border-emerald-700/50 bg-emerald-600/10 rounded p-3 text-sm">
        <div className="flex items-center justify-between gap-3">
          <span>
            <ShieldCheck size={14} className="inline mr-1.5 text-emerald-400" />
            Enforcement is on — {(g?.suppressions ?? 0).toLocaleString()} expensive
            looks skipped so far.
          </span>
          <Button variant="ghost" className="text-xs shrink-0"
            disabled={setGate.isPending}
            onClick={() => setGate.mutate('shadow')}>
            Revert to shadow
          </Button>
        </div>
      </div>
    )
  }

  if (d.mode !== 'shadow') return null

  // ready — the recommendation with evidence
  if (promo.ready) {
    return (
      <div className="border border-[var(--accent,#5eb3f6)]/60 bg-[var(--accent,#5eb3f6)]/10 rounded p-3 text-sm space-y-2">
        <div>
          <Info size={14} className="inline mr-1.5" />
          Shadow data ({promo.shadow_days?.toFixed(0)} days): enforcement would
          have skipped <strong>{formatPct(promo.would_save_ratio)}</strong> of
          expensive model calls. Critical classes (e.g. person) always escalate.
        </div>
        <Button variant="primary" disabled={setGate.isPending}
          onClick={() => setGate.mutate('enforce')}>
          {setGate.isPending ? 'Enabling…' : 'Enable enforcement'}
        </Button>
      </div>
    )
  }

  // collecting — progress doubles as the in-product Tier-0 explainer
  const days = promo.shadow_days ?? 0
  return (
    <div className="border border-[var(--border)] rounded bg-[var(--bg-2)] p-3 text-xs text-[var(--text-dim)]">
      Measuring in shadow — day {Math.max(1, Math.ceil(days))} of 7. The gate is
      auditing every escalate/suppress decision without acting; once a week of
      data shows meaningful savings, you can enable enforcement here with one
      click.
      {promo.would_save_ratio != null && (
        <> So far it would skip <strong className="text-[var(--text)]">{formatPct(promo.would_save_ratio)}</strong> of
        expensive calls.</>
      )}
    </div>
  )
}

function ComputeGatedPanel() {
  const query = useTier0Metrics()
  const d = query.data

  // Absent / unreachable pipeline is a normal state (gate off / not deployed).
  if (query.isError || (d && !d.available)) {
    const reason = d?.reason === 'disabled'
      ? 'Disabled — set detect_pipeline_metrics_url to enable.'
      : 'The detect-pipeline is not reachable — the compute-gated pipeline may be off or not deployed.'
    return (
      <Card>
        <CardHeader>
          <Activity size={16} className="text-[var(--text-dim)]" />
          <CardTitle>Compute-gated inference</CardTitle>
          <div className="ml-auto"><Badge variant="neutral">unavailable</Badge></div>
        </CardHeader>
        <CardContent><div className="text-sm text-[var(--text-dim)]">{reason}</div></CardContent>
      </Card>
    )
  }

  if (query.isPending || !d) {
    return <Skeleton className="h-40" />
  }

  const mode = TIER0_MODE_LABEL[d.mode ?? 'not_running'] ?? TIER0_MODE_LABEL.not_running
  const f = d.frames
  const g = d.gate
  const t = d.tier1
  const p = d.process
  const shadow = d.mode === 'shadow'
  // In shadow the gate acts on nothing, so the "would-suppress" count is the risk-free
  // preview of what enforce would save; in enforce, suppressions are real.
  const suppressN = shadow ? (g?.shadow_would_suppress ?? 0) : (g?.suppressions ?? 0)
  const escN = g?.escalations ?? 0
  const decisionTotal = suppressN + escN

  return (
    <Card>
      <CardHeader>
        <Activity size={16} className="text-[var(--text-dim)]" />
        <CardTitle>Compute-gated inference</CardTitle>
        <div className="ml-auto flex items-center gap-2">
          {d.model && <Badge variant="info"><Cpu size={12} /> {d.model}</Badge>}
          <Badge variant={mode.variant}>{mode.text}</Badge>
          {query.isFetching && <RefreshCw size={13} className="animate-spin text-[var(--text-dim)]" />}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <PromotionCard d={d} />
        {/* Operator health — is it running, and is the box keeping up with the cameras? */}
        {d.health && (d.health.workers_total > 0 || d.health.restarts_total > 0) && (
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
            <StatTile label="Workers up" value={`${d.health.workers_up} / ${d.health.workers_total}`}
              sub="analyze-enabled cameras" warn={d.health.workers_up < d.health.workers_total} />
            <StatTile label="Keeping up"
              value={d.health.min_fps_ratio != null ? formatPct(d.health.min_fps_ratio) : '—'}
              sub={d.health.worst_camera ? `worst: ${d.health.worst_camera}` : 'processed ÷ target fps'}
              warn={d.health.min_fps_ratio != null && d.health.min_fps_ratio < 0.9} />
            <StatTile label="Restarts" value={(d.health.restarts_total ?? 0).toLocaleString()}
              sub="camera feed restarts" warn={(d.health.restarts_total ?? 0) > 0} />
          </div>
        )}

        {/* Process resource use — same signals the adapter cards show, side by side */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          <StatTile label="CPU" value={p?.cpu_percent != null ? `${Math.round(p.cpu_percent)}%` : '—'} sub="detect-pipeline process" />
          <StatTile label="Memory" value={formatBytes(p?.memory_bytes)} sub="resident" />
          <StatTile label="Motion-gate" value={formatPct(f?.motion_gate_ratio)}
            sub={`${(f?.skipped_no_motion ?? 0).toLocaleString()} idle frames skipped`} />
          <StatTile label="Frames" value={(f?.total ?? 0).toLocaleString()}
            sub={`${(f?.detector_runs ?? 0).toLocaleString()} ran the detector`} />
        </div>

        {/* Detector model — speed + output volume, the aspects you A/B two models on */}
        {d.detector && (
          <div className="border border-[var(--border)] rounded bg-[var(--bg-2)] p-3">
            <div className="text-[11px] uppercase tracking-wider text-[var(--text-dim)] mb-2 font-mono">
              Detector model{d.model ? ` — ${d.model}` : ''}
            </div>
            <div className="flex flex-wrap items-center gap-x-6 gap-y-2 font-mono text-xs">
              <span>
                <span className="text-[var(--text-dim)]">inference </span>
                <span className="font-bold tabular-nums">{formatMs(d.detector.latency_avg_ms)}</span>
                {d.detector.latency_p95_ms != null && (
                  <span className="text-[var(--text-dim)]"> · p95 {formatMs(d.detector.latency_p95_ms)}</span>
                )}
              </span>
              <span>
                <span className="font-bold tabular-nums">{(d.detector.detections_total ?? 0).toLocaleString()}</span>
                <span className="text-[var(--text-dim)]"> detections</span>
              </span>
              {Object.entries(d.detector.detections_by_class ?? {})
                .sort((a, b) => b[1] - a[1]).slice(0, 6)
                .map(([label, n]) => (
                  <span key={label} className="inline-flex items-center gap-1.5 text-[var(--text-dim)]">
                    <i className="w-1.5 h-1.5 rounded-sm bg-[var(--accent,#5eb3f6)]" />
                    {label} <span className="tabular-nums text-[var(--text)]">{n.toLocaleString()}</span>
                  </span>
                ))}
            </div>
            {d.detector.stage_latency_ms && Object.keys(d.detector.stage_latency_ms).length > 0 && (
              <div className="mt-2 pt-2 border-t border-[var(--border)] flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-[var(--text-dim)]">
                <span className="uppercase tracking-wider">per-stage avg:</span>
                {['decode', 'motion', 'region', 'detect', 'track']
                  .filter((st) => d.detector!.stage_latency_ms[st] != null)
                  .map((st) => (
                    <span key={st}>{st} <span className="tabular-nums text-[var(--text)]">{formatMs(d.detector!.stage_latency_ms[st])}</span></span>
                  ))}
              </div>
            )}
          </div>
        )}

        {/* Gate decisions: what got suppressed (the saving) vs escalated (the cost) */}
        <div className="border border-[var(--border)] rounded bg-[var(--bg-2)] p-3">
          <div className="text-[11px] uppercase tracking-wider text-[var(--text-dim)] mb-2 font-mono">
            Gate decisions{shadow ? ' — shadow preview' : ''}
          </div>
          {decisionTotal === 0 ? (
            <div className="text-xs text-[var(--text-dim)]">No gate decisions recorded yet.</div>
          ) : (
            <>
              <div className="flex h-2.5 rounded overflow-hidden mb-2">
                <div className="bg-emerald-500" style={{ width: `${(suppressN / decisionTotal) * 100}%` }}
                  title={`${shadow ? 'would suppress' : 'suppressed'}: ${suppressN}`} />
                <div className="bg-amber-500" style={{ width: `${(escN / decisionTotal) * 100}%` }}
                  title={`escalated: ${escN}`} />
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-[var(--text-dim)]">
                <span className="inline-flex items-center gap-1.5">
                  <i className="w-2 h-2 rounded-sm bg-emerald-500" />
                  {shadow ? 'would suppress' : 'suppressed'} {suppressN.toLocaleString()} ({formatPct(suppressN / decisionTotal)})
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <i className="w-2 h-2 rounded-sm bg-amber-500" />
                  escalated {escN.toLocaleString()} ({formatPct(escN / decisionTotal)})
                </span>
              </div>
            </>
          )}
        </div>

        {/* Expensive-model (Tier-1) calls — how often the costly path runs + its cost */}
        <div>
          <div className="text-[11px] uppercase tracking-wider text-[var(--text-dim)] mb-2 font-mono">
            Expensive-model calls (Tier-1)
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <StatTile label="Dispatched" value={(t?.dispatched ?? 0).toLocaleString()} sub="governed via KAI-C" />
            <StatTile label="Latency" value={formatMs(t?.latency_avg_ms)}
              sub={t?.latency_p95_ms != null ? `p95 ${formatMs(t.latency_p95_ms)}` : 'avg per call'} />
            <StatTile label="In flight" value={`${t?.inflight ?? 0}`} sub="concurrent now" />
            <StatTile label="Errors / dropped" value={`${t?.errors ?? 0} / ${t?.dropped ?? 0}`}
              sub="failed · shed under load" warn={(t?.errors ?? 0) > 0 || (t?.dropped ?? 0) > 0} />
          </div>
          {shadow && (
            <div className="mt-2 text-[11px] text-[var(--text-dim)]">
              In shadow mode the gate dispatches nothing — enable <span className="font-mono">enforce</span> + dispatch to run the routed model.
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

export function AIAdapters() {
  const healthQuery = useKaiHealth()
  const capsQuery = useKaiCapabilities()
  const fleetQuery = useFleetMetrics()
  const [expanded, setExpanded] = useState<string | null>(null)

  const loading = healthQuery.isPending || capsQuery.isPending
  const bothFailed = healthQuery.isError && capsQuery.isError

  const adapterNames = new Set<string>([
    ...Object.keys(capsQuery.data?.adapters ?? {}),
    ...Object.keys(healthQuery.data?.adapters ?? {}),
  ])
  const adapters = Array.from(adapterNames)
    .sort()
    .map((name) => summarizeAdapter(name, capsQuery.data?.adapters?.[name], healthQuery.data?.adapters?.[name]))

  const refresh = () => {
    healthQuery.refetch()
    capsQuery.refetch()
  }

  return (
    <section className="space-y-4">
      <PageHeader
        title="AI Adapters"
        description="Models registered with KAI-C, the sovereignty and audit gateway. Every inference the platform runs goes through one of these adapters. Health & metrics update on KAI-C's 60s scrape."
        actions={
          <Button onClick={refresh} disabled={loading}>
            <RefreshCw size={14} className={healthQuery.isFetching || capsQuery.isFetching ? 'animate-spin' : ''} /> Refresh
          </Button>
        }
      />

      <FleetStrip fleet={fleetQuery.data} />

      <ComputeGatedPanel />

      {/* KAI-C gateway status */}
      <Card>
        <CardHeader>
          <Server size={16} className="text-[var(--text-dim)]" />
          <CardTitle>KAI-C Gateway</CardTitle>
          <div className="ml-auto">
            {healthQuery.isPending ? (
              <Skeleton className="h-5 w-16" />
            ) : (
              <Badge variant={statusVariant(healthQuery.data?.kai_c_status ?? (healthQuery.isError ? 'error' : undefined))}>
                {healthQuery.data?.kai_c_status ?? (healthQuery.isError ? 'unreachable' : 'unknown')}
              </Badge>
            )}
          </div>
        </CardHeader>
        {(healthQuery.data?.message || healthQuery.isError) && (
          <CardContent>
            <div className="text-sm text-[var(--text-dim)]">
              {healthQuery.data?.message ?? extractApiError(healthQuery.error, 'KAI-C is not reachable from the backend.')}
            </div>
          </CardContent>
        )}
      </Card>

      {/* Adapters */}
      {loading ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-40" />
          ))}
        </div>
      ) : bothFailed ? (
        <ErrorCard
          title="Adapter registry unavailable"
          message={extractApiError(capsQuery.error, 'Could not load adapter capabilities from KAI-C.')}
          onRetry={refresh}
        />
      ) : adapters.length === 0 ? (
        <EmptyState
          icon={<Layers size={28} />}
          title="No adapters registered"
          description="Start an AI adapter (YOLOv8, BLIP, Whisper, …) and register it with KAI-C to see it here. See docs/AI_ADAPTER_CONTRACT.md for the contract."
        />
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {adapters.map((a) => (
            <Card key={a.name}>
              <CardHeader>
                <Layers size={16} className="text-[var(--text-dim)]" />
                <CardTitle>{a.name}</CardTitle>
                <div className="ml-auto flex items-center gap-2">
                  <AdapterApprovalBadge name={a.name} />
                  {a.status && <Badge variant={statusVariant(a.status)}>{a.status}</Badge>}
                </div>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                  <div className="text-[var(--text-dim)]">Model</div>
                  <div>{a.modelName ?? '—'}{a.modelVersion ? ` · v${a.modelVersion}` : ''}</div>
                  {a.framework && (
                    <>
                      <div className="text-[var(--text-dim)]">Framework</div>
                      <div>{a.framework}</div>
                    </>
                  )}
                  {a.fingerprint && (
                    <>
                      <div className="text-[var(--text-dim)]">Fingerprint</div>
                      <div className="font-mono text-xs truncate" title={a.fingerprint}>{a.fingerprint}</div>
                    </>
                  )}
                </div>

                {a.tasks.length > 0 && (
                  <div>
                    <div className="text-[var(--text-dim)] mb-1">Tasks</div>
                    <div className="flex flex-wrap gap-1">
                      {a.tasks.map((t) => (
                        <Badge key={t} variant="info">{t}</Badge>
                      ))}
                    </div>
                  </div>
                )}

                {a.requestedPerms.length > 0 && (
                  <div>
                    <div className="text-[var(--text-dim)] mb-1 flex items-center gap-1">
                      <ShieldAlert size={12} /> Requested permissions
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {a.requestedPerms.map((p) => (
                        <Badge key={p} variant="warning">{p}</Badge>
                      ))}
                    </div>
                  </div>
                )}

                <AdapterPermissionsSection name={a.name} />

                <AdapterMetricsSection name={a.name} />

                <div>
                  <Button variant="ghost" className="text-xs px-2 py-1" onClick={() => setExpanded(expanded === a.name ? null : a.name)}>
                    {expanded === a.name ? 'Hide raw capabilities' : 'Show raw capabilities'}
                  </Button>
                  {expanded === a.name && (
                    <pre className="mt-2 p-3 text-xs leading-snug overflow-auto max-h-64 border border-[var(--border)] rounded bg-[var(--bg-2)]">
                      {JSON.stringify(a.raw, null, 2)}
                    </pre>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </section>
  )
}
