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

// Vehicles — the first-class LPR vertical (the commercial surface).
//
// Capability-keyed, not app-keyed: this page appears when an enabled
// catalog app provides the license_plate_recognition capability
// (AppShell gates the nav entry the same way), so a community LPR app
// lights the same page. Data comes from the PLATFORM, not the app:
// plate reads are timeline visits (plate_text + best-frame evidence),
// written whichever producer ran the OCR. Watchlists write through the
// providing app's config endpoint — the same live-update path the
// catalog form uses.

import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BellRing, Car, Download, History, Plus, RefreshCw, Search,
  ShieldAlert, ShieldCheck, Trash2, Upload,
} from 'lucide-react'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { AuthedImage } from '../components/AuthedImage'
import { Modal } from '../components/Modal'
import { useSnackbar } from '../components/Snackbar'
import {
  Badge, Button, Card, CardContent,
  EmptyState, ErrorCard, PageHeader, Skeleton,
} from '../components/ui'
import type { RegisteredApp } from './AppCatalog'

export const LPR_TASK = 'license_plate_recognition'

type PlateEvent = {
  id: number
  camera_id: number
  label?: string | null
  plate_text?: string | null
  started_at?: string | null
  ended_at?: string | null
  has_evidence?: boolean
  evidence_url?: string | null
}

type PlateStats = {
  days: number
  total_reads: number
  unique_plates: number
  per_camera: { camera_id: number; reads: number }[]
  per_day: { day: string; reads: number }[]
}

type CameraRow = { id: number; name: string }

type PlateSummary = {
  plate: string
  total_reads: number
  first_seen: string | null
  last_seen: string | null
  per_camera: { camera_id: number; reads: number }[]
}

/** The enabled app providing LPR — capability-keyed (community-proof). */
export function findLprApp(apps: RegisteredApp[] | undefined): RegisteredApp | null {
  return (
    (apps ?? []).find(
      (a) => a.enabled && (a.manifest?.requires_tasks ?? []).includes(LPR_TASK)
    ) ?? null
  )
}

/** One row of the society's vehicle register (app config `registry`). */
export type RegistryEntry = {
  plate: string
  owner?: string
  unit?: string
  type?: string
  note?: string
}

/** The platform's plate normalisation: upper-case, no separators. */
export function normalizePlate(v: string): string {
  return v.split(/\s+/).join('').toUpperCase()
}

/** Tolerant read of the app's registry config (strings or records). */
export function parseRegistry(raw: unknown): RegistryEntry[] {
  if (!Array.isArray(raw)) return []
  const out: RegistryEntry[] = []
  for (const e of raw) {
    if (typeof e === 'string') {
      const plate = normalizePlate(e)
      if (plate) out.push({ plate })
    } else if (e && typeof e === 'object') {
      const plate = normalizePlate(String((e as any).plate ?? ''))
      if (!plate) continue
      const r: RegistryEntry = { plate }
      for (const k of ['owner', 'unit', 'type', 'note'] as const) {
        const v = String((e as any)[k] ?? '').trim()
        if (v) r[k] = v
      }
      out.push(r)
    }
  }
  return out
}

/** Minimal CSV parse (quoted fields supported) → registry entries.
 * With a header row, columns are matched by name; without one, the
 * first column is the plate. */
export function registryFromCsv(text: string): RegistryEntry[] {
  const rows: string[][] = []
  let field = '', row: string[] = [], inQ = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (inQ) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++ }
      else if (c === '"') inQ = false
      else field += c
    } else if (c === '"') inQ = true
    else if (c === ',') { row.push(field); field = '' }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++
      row.push(field); field = ''
      if (row.some((f) => f.trim())) rows.push(row)
      row = []
    } else field += c
  }
  row.push(field)
  if (row.some((f) => f.trim())) rows.push(row)
  if (!rows.length) return []

  const KNOWN = ['plate', 'owner', 'unit', 'type', 'note']
  const header = rows[0].map((h) => h.trim().toLowerCase())
  const hasHeader = header.includes('plate')
  const cols = hasHeader
    ? header.map((h) => (KNOWN.includes(h) ? h : null))
    : ['plate', 'owner', 'unit', 'type', 'note']
  const body = hasHeader ? rows.slice(1) : rows

  const out: RegistryEntry[] = []
  for (const r of body) {
    const rec: Record<string, string> = {}
    r.forEach((v, i) => {
      const k = cols[i]
      if (k && v.trim()) rec[k] = v.trim()
    })
    const plate = normalizePlate(rec.plate ?? '')
    if (!plate) continue
    const entry: RegistryEntry = { plate }
    for (const k of ['owner', 'unit', 'type', 'note'] as const) {
      if (rec[k]) entry[k] = rec[k]
    }
    out.push(entry)
  }
  return out
}

function registryToCsv(entries: RegistryEntry[]): string {
  const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
  return [
    'plate,owner,unit,type,note',
    ...entries.map((e) => [e.plate, e.owner, e.unit, e.type, e.note].map(esc).join(',')),
  ].join('\n')
}

const RANGE_PRESETS = [
  { key: '24h', label: 'Last 24h', hours: 24 },
  { key: '7d', label: '7 days', hours: 24 * 7 },
  { key: '30d', label: '30 days', hours: 24 * 30 },
] as const

function toCsv(rows: PlateEvent[], cameraName: (id: number) => string): string {
  const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
  const head = 'plate,camera,seen_at,left_at,label'
  const body = rows.map((r) =>
    [r.plate_text, cameraName(r.camera_id), r.started_at, r.ended_at, r.label]
      .map(esc).join(','))
  return [head, ...body].join('\n')
}

export function Vehicles() {
  const queryClient = useQueryClient()
  const { showSuccess, showError } = useSnackbar()
  const [plate, setPlate] = useState('')
  const [cameraId, setCameraId] = useState<number | ''>('')
  const [range, setRange] = useState<(typeof RANGE_PRESETS)[number]>(RANGE_PRESETS[0])
  const [preview, setPreview] = useState<PlateEvent | null>(null)
  const [tab, setTab] = useState<'reads' | 'registry'>('reads')
  const [historyPlate, setHistoryPlate] = useState<string | null>(null)

  const camerasQuery = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const { data } = await apiService.getCameras()
      const list = Array.isArray(data) ? data : (data as any)?.cameras
      return (Array.isArray(list) ? list : []) as CameraRow[]
    },
    retry: 0,
  })
  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
  })

  const fromIso = useMemo(
    () => new Date(Date.now() - range.hours * 3600 * 1000).toISOString(),
    // Re-anchor when the preset changes; a stable anchor per selection
    // keeps the query key stable between refetch intervals.
    [range]
  )

  const eventsQuery = useQuery({
    queryKey: ['plate-events', plate, cameraId, range.key],
    queryFn: async () => {
      const { data } = await apiService.getPlateEvents({
        plate: plate.trim() || undefined,
        camera_id: cameraId === '' ? undefined : cameraId,
        from: fromIso,
        limit: 200,
      })
      return ((data as any)?.events ?? []) as PlateEvent[]
    },
    retry: 0,
    refetchInterval: 30_000,
  })

  const statsQuery = useQuery({
    queryKey: ['plate-stats'],
    queryFn: async () => {
      const { data } = await apiService.getPlateStats(7)
      return data as PlateStats
    },
    retry: 0,
    refetchInterval: 60_000,
  })

  const lprApp = findLprApp(appsQuery.data)
  const allow: string[] = (lprApp?.config as any)?.allowlist ?? []
  const deny: string[] = (lprApp?.config as any)?.denylist ?? []

  // The society register + alarm mode live in the providing app's
  // config, exactly like the watchlists (live-applied by the app).
  const registry = useMemo(
    () => parseRegistry((lprApp?.config as any)?.registry),
    [lprApp]
  )
  const registryPlates = useMemo(
    () => new Set(registry.map((r) => r.plate)),
    [registry]
  )
  const alarmOnUnknown = Boolean((lprApp?.config as any)?.alarm_on_unknown)

  const saveConfig = useMutation({
    mutationFn: async (patch: Record<string, any>) => {
      if (!lprApp) throw new Error('No enabled LPR app to hold the register.')
      await apiService.updateAppConfig(lprApp.id, {
        ...((lprApp.config ?? {}) as Record<string, any>),
        ...patch,
      })
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['apps'] }),
    onError: (e) => showError(extractApiError(e, 'Could not save the register.')),
  })

  // All-time history for one plate (the drill-down modal).
  const historyQuery = useQuery({
    queryKey: ['plate-summary', historyPlate],
    queryFn: async () => {
      const { data } = await apiService.getPlateSummary(historyPlate as string)
      return data as PlateSummary
    },
    enabled: Boolean(historyPlate),
    retry: 0,
  })

  // Add-to-watchlist writes through the providing app's config endpoint
  // (the app applies watchlists LIVE — the same path the catalog form
  // uses), merging over the current config so nothing else is lost.
  const watchlist = useMutation({
    mutationFn: async ({ plateText, list }: { plateText: string; list: 'allowlist' | 'denylist' }) => {
      if (!lprApp) throw new Error('No enabled LPR app to hold the watchlist.')
      const cfg = { ...(lprApp.config ?? {}) } as Record<string, any>
      const current: string[] = Array.isArray(cfg[list]) ? cfg[list] : []
      if (current.includes(plateText)) return
      cfg[list] = [...current, plateText]
      await apiService.updateAppConfig(lprApp.id, cfg)
    },
    onSuccess: (_d, vars) => {
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      showSuccess(`${vars.plateText} added to the ${vars.list}`)
    },
    onError: (e) => showError(extractApiError(e, 'Could not update the watchlist.')),
  })

  const cameraName = (id: number) =>
    camerasQuery.data?.find((c) => c.id === id)?.name ?? `cam${id}`

  const exportCsv = () => {
    const rows = eventsQuery.data ?? []
    const blob = new Blob([toCsv(rows, cameraName)], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `plate-reads-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  const stats = statsQuery.data
  const events = eventsQuery.data ?? []

  return (
    <section className="space-y-4">
      <PageHeader
        title="Vehicles"
        description="License plate reads across your cameras — searched from the evidence store, whichever part of the platform ran the OCR. Watchlists apply live."
        actions={
          <div className="flex items-center gap-2">
            <Button variant="outline" onClick={exportCsv} disabled={!events.length}>
              <Download size={14} /> Export CSV
            </Button>
            <Button onClick={() => eventsQuery.refetch()} disabled={eventsQuery.isFetching}>
              <RefreshCw size={14} className={eventsQuery.isFetching ? 'animate-spin' : ''} /> Refresh
            </Button>
          </div>
        }
      />

      {/* ── Stat tiles (7-day window) ─────────────────────────────── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {[
          { label: 'Reads (7d)', value: stats?.total_reads },
          { label: 'Unique plates (7d)', value: stats?.unique_plates },
          { label: 'Busiest camera (7d)', value: stats?.per_camera?.length
              ? cameraName([...stats.per_camera].sort((a, b) => b.reads - a.reads)[0].camera_id)
              : '—' },
          { label: 'Registered vehicles', value: lprApp ? registry.length : '—' },
        ].map((t) => (
          <Card key={t.label}>
            <CardContent className="py-3">
              <div className="text-2xl font-semibold">{t.value ?? '…'}</div>
              <div className="text-xs text-[var(--text-dim)]">{t.label}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* ── Tabs: live reads vs the vehicle register ─────────────── */}
      <div className="flex items-center gap-1 border-b border-[var(--border)]">
        {([
          { key: 'reads', label: 'Plate reads' },
          { key: 'registry', label: `Vehicle register (${registry.length})` },
        ] as const).map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-3 py-2 text-sm -mb-px border-b-2 ${tab === t.key
              ? 'border-[var(--accent,var(--text))] text-[var(--text)] font-medium'
              : 'border-transparent text-[var(--text-dim)] hover:text-[var(--text)]'}`}
          >
            {t.label}
          </button>
        ))}
        {alarmOnUnknown && (
          <span className="ml-auto inline-flex items-center gap-1 text-xs text-[var(--warning,#b7791f)]">
            <BellRing size={13} /> Unknown-vehicle alarm is ON
          </span>
        )}
      </div>

      {tab === 'registry' ? (
        <RegistryTab
          registry={registry}
          alarmOnUnknown={alarmOnUnknown}
          canEdit={Boolean(lprApp)}
          saving={saveConfig.isPending}
          onSaveRegistry={(entries) => {
            saveConfig.mutate({ registry: entries }, {
              onSuccess: () => showSuccess(`Register saved — ${entries.length} vehicles`),
            })
          }}
          onToggleAlarm={(on) => {
            saveConfig.mutate({ alarm_on_unknown: on }, {
              onSuccess: () => showSuccess(
                on ? 'Unknown-vehicle alarm enabled — strangers now raise a high-severity alert'
                   : 'Unknown-vehicle alarm disabled'),
            })
          }}
        />
      ) : (
      <>
      {/* ── Filters ───────────────────────────────────────────────── */}
      <Card>
        <CardContent className="py-3 flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-[var(--text-dim)]" />
            <input
              value={plate}
              onChange={(e) => setPlate(e.target.value)}
              placeholder="Plate contains… (e.g. 1234)"
              className="pl-7 pr-2 py-1.5 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm w-56"
            />
          </div>
          <select
            value={cameraId}
            onChange={(e) => setCameraId(e.target.value === '' ? '' : Number(e.target.value))}
            className="py-1.5 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm"
          >
            <option value="">All cameras</option>
            {(camerasQuery.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
          <div className="flex rounded border border-[var(--border)] overflow-hidden">
            {RANGE_PRESETS.map((r) => (
              <button
                key={r.key}
                onClick={() => setRange(r)}
                className={`px-3 py-1.5 text-sm ${range.key === r.key
                  ? 'bg-[var(--accent,var(--bg-2))] text-white'
                  : 'bg-[var(--bg-2)] text-[var(--text-dim)]'}`}
              >
                {r.label}
              </button>
            ))}
          </div>
          {!lprApp && (
            <span className="text-xs text-[var(--text-dim)] ml-auto">
              No enabled LPR app — reads still collect; watchlists need the app.
            </span>
          )}
        </CardContent>
      </Card>

      {/* ── Reads table ───────────────────────────────────────────── */}
      {eventsQuery.isPending ? (
        <Skeleton className="h-64" />
      ) : eventsQuery.isError ? (
        <ErrorCard
          title="Could not load plate reads"
          message={extractApiError(eventsQuery.error, 'The events store is unreachable.')}
          onRetry={() => eventsQuery.refetch()}
        />
      ) : events.length === 0 ? (
        <EmptyState
          icon={<Car size={28} />}
          title="No plate reads in this window"
          description="Assign cameras the License Plate Recognition skill (Cameras → edit → Assignments) and vehicle visits will appear here with their evidence photos."
        />
      ) : (
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-[var(--text-dim)] border-b border-[var(--border)]">
                  <th className="px-3 py-2">Photo</th>
                  <th className="px-3 py-2">Plate</th>
                  <th className="px-3 py-2">Camera</th>
                  <th className="px-3 py-2">Seen</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2 text-right pr-4">Watchlist</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e) => {
                  const p = (e.plate_text ?? '').toUpperCase()
                  const inDeny = deny.includes(p)
                  const inAllow = allow.includes(p)
                  const registered = registryPlates.has(p)
                  return (
                    <tr key={e.id} className="border-b border-[var(--border)] last:border-0 hover:bg-[var(--bg-2)]">
                      <td className="px-3 py-1.5">
                        {e.has_evidence ? (
                          <AuthedImage
                            queryKey={['evidence', e.id]}
                            fetchBlob={() => apiService.getEventEvidence(e.id)}
                            alt={`evidence for ${p}`}
                            className="h-10 w-16 object-cover rounded cursor-zoom-in"
                            onClick={() => setPreview(e)}
                          />
                        ) : (
                          <div className="h-10 w-16 grid place-items-center text-[10px] text-[var(--text-dim)] bg-[var(--bg-2)] rounded">—</div>
                        )}
                      </td>
                      <td className="px-3 py-1.5 font-mono font-semibold">
                        <button
                          className="hover:underline inline-flex items-center gap-1"
                          title="Vehicle history — every time this plate was seen"
                          onClick={() => setHistoryPlate(p)}
                        >
                          {p} <History size={12} className="text-[var(--text-dim)]" />
                        </button>
                      </td>
                      <td className="px-3 py-1.5">{cameraName(e.camera_id)}</td>
                      <td className="px-3 py-1.5 text-[var(--text-dim)]">
                        {e.started_at ? new Date(e.started_at).toLocaleString() : '—'}
                      </td>
                      <td className="px-3 py-1.5">
                        {inDeny ? <Badge variant="destructive">watchlist</Badge>
                          : registered ? <Badge variant="success">registered</Badge>
                          : inAllow ? <Badge variant="success">expected</Badge>
                          : alarmOnUnknown ? <Badge variant="warning">unknown</Badge>
                          : <Badge variant="neutral">{e.label || 'vehicle'}</Badge>}
                      </td>
                      <td className="px-3 py-1.5 text-right pr-4 whitespace-nowrap">
                        {lprApp && !inDeny && (
                          <button
                            title="Alert at high severity when this plate is seen"
                            className="text-[var(--text-dim)] hover:text-[var(--text)] mr-2"
                            onClick={() => watchlist.mutate({ plateText: p, list: 'denylist' })}
                          >
                            <ShieldAlert size={15} />
                          </button>
                        )}
                        {lprApp && !inAllow && (
                          <button
                            title="Mark as an expected vehicle (low-severity reads)"
                            className="text-[var(--text-dim)] hover:text-[var(--text)]"
                            onClick={() => watchlist.mutate({ plateText: p, list: 'allowlist' })}
                          >
                            <ShieldCheck size={15} />
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}
      </>
      )}

      {/* ── Per-plate history (all-time) ──────────────────────────── */}
      {historyPlate && (
        <Modal open onClose={() => setHistoryPlate(null)} title={`${historyPlate} — vehicle history`}>
          {historyQuery.isPending ? (
            <Skeleton className="h-24" />
          ) : historyQuery.isError ? (
            <div className="text-sm text-[var(--text-dim)]">
              {extractApiError(historyQuery.error, 'Could not load this plate’s history.')}
            </div>
          ) : (
            <div className="space-y-3 text-sm">
              {(() => {
                const reg = registry.find((r) => r.plate === historyPlate)
                return reg && (reg.owner || reg.unit || reg.type || reg.note) ? (
                  <div className="rounded border border-[var(--border)] bg-[var(--bg-2)] px-3 py-2">
                    Registered{reg.owner ? ` to ${reg.owner}` : ''}
                    {reg.unit ? ` · ${reg.unit}` : ''}
                    {reg.type ? ` · ${reg.type}` : ''}
                    {reg.note ? ` — ${reg.note}` : ''}
                  </div>
                ) : null
              })()}
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <div className="text-xl font-semibold">{historyQuery.data?.total_reads ?? 0}</div>
                  <div className="text-xs text-[var(--text-dim)]">total reads</div>
                </div>
                <div>
                  <div className="font-medium">
                    {historyQuery.data?.first_seen
                      ? new Date(historyQuery.data.first_seen).toLocaleString() : '—'}
                  </div>
                  <div className="text-xs text-[var(--text-dim)]">first seen</div>
                </div>
                <div>
                  <div className="font-medium">
                    {historyQuery.data?.last_seen
                      ? new Date(historyQuery.data.last_seen).toLocaleString() : '—'}
                  </div>
                  <div className="text-xs text-[var(--text-dim)]">last seen</div>
                </div>
              </div>
              {(historyQuery.data?.per_camera ?? []).length > 0 && (
                <div>
                  <div className="text-xs text-[var(--text-dim)] mb-1">By camera</div>
                  {(historyQuery.data?.per_camera ?? []).map((c) => (
                    <div key={c.camera_id} className="flex justify-between border-b border-[var(--border)] last:border-0 py-1">
                      <span>{cameraName(c.camera_id)}</span>
                      <span className="font-mono">{c.reads}</span>
                    </div>
                  ))}
                </div>
              )}
              <Button
                variant="outline"
                onClick={() => {
                  setPlate(historyPlate)
                  setTab('reads')
                  setHistoryPlate(null)
                }}
              >
                <Search size={14} /> Show these reads
              </Button>
            </div>
          )}
        </Modal>
      )}

      {/* ── Evidence preview ──────────────────────────────────────── */}
      {preview && (
        <Modal open onClose={() => setPreview(null)} title={`${(preview.plate_text ?? '').toUpperCase()} — ${cameraName(preview.camera_id)}`}>
          <AuthedImage
            queryKey={['evidence', preview.id]}
            fetchBlob={() => apiService.getEventEvidence(preview.id)}
            alt="evidence"
            className="max-h-[70vh] w-auto rounded"
          />
          <div className="text-xs text-[var(--text-dim)] mt-2">
            {preview.started_at ? new Date(preview.started_at).toLocaleString() : ''}
          </div>
        </Modal>
      )}
    </section>
  )
}

// ── The vehicle register (society mode) ─────────────────────────────
// The register lives in the providing app's config; edits here write
// through the same live-update path as the watchlists. CSV import is
// deliberate: a society secretary has 300 plates in a spreadsheet, and
// "export" doubles as the import template.

function RegistryTab({
  registry,
  alarmOnUnknown,
  canEdit,
  saving,
  onSaveRegistry,
  onToggleAlarm,
}: {
  registry: RegistryEntry[]
  alarmOnUnknown: boolean
  canEdit: boolean
  saving: boolean
  onSaveRegistry: (entries: RegistryEntry[]) => void
  onToggleAlarm: (on: boolean) => void
}) {
  const [draft, setDraft] = useState<RegistryEntry>({ plate: '' })
  const fileRef = useRef<HTMLInputElement | null>(null)

  const addDraft = () => {
    const plate = normalizePlate(draft.plate)
    if (!plate) return
    const entry: RegistryEntry = { plate }
    for (const k of ['owner', 'unit', 'type', 'note'] as const) {
      const v = (draft[k] ?? '').trim()
      if (v) entry[k] = v
    }
    onSaveRegistry([...registry.filter((r) => r.plate !== plate), entry])
    setDraft({ plate: '' })
  }

  const importCsv = (file: File) => {
    const reader = new FileReader()
    reader.onload = () => {
      const imported = registryFromCsv(String(reader.result ?? ''))
      if (!imported.length) return
      // Imported rows win over existing ones with the same plate.
      const merged = new Map(registry.map((r) => [r.plate, r] as const))
      for (const e of imported) merged.set(e.plate, e)
      onSaveRegistry([...merged.values()])
    }
    reader.readAsText(file)
  }

  const exportCsv = () => {
    const blob = new Blob([registryToCsv(registry)], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = 'vehicle-register.csv'
    a.click()
    URL.revokeObjectURL(a.href)
  }

  if (!canEdit) {
    return (
      <EmptyState
        icon={<Car size={28} />}
        title="The register needs an enabled LPR app"
        description="Install and enable a License Plate Recognition app from the App Catalog — the vehicle register and the unknown-vehicle alarm live in that app and apply live."
      />
    )
  }

  return (
    <div className="space-y-4">
      {/* Alarm mode */}
      <Card>
        <CardContent className="py-3 flex flex-wrap items-center gap-3">
          <BellRing size={18} className={alarmOnUnknown ? 'text-[var(--warning,#b7791f)]' : 'text-[var(--text-dim)]'} />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium">Alarm on unknown vehicles</div>
            <div className="text-xs text-[var(--text-dim)]">
              Any plate not in this register (or the allowlist) raises a high-severity
              alert the moment it is read — one alarm per stranger, across all gate
              cameras. Watchlisted plates keep their own alarm.
            </div>
          </div>
          <Button
            // The shared 'danger' variant is dark-theme-tuned and washes
            // out on light; an outlined red reads in both themes.
            variant={alarmOnUnknown ? 'outline' : 'primary'}
            className={alarmOnUnknown
              ? 'text-[var(--danger,#e5484d)] border-[var(--danger,#e5484d)]' : ''}
            onClick={() => onToggleAlarm(!alarmOnUnknown)}
            disabled={saving}
          >
            {alarmOnUnknown ? 'Turn off' : 'Turn on'}
          </Button>
        </CardContent>
      </Card>

      {/* Add + import */}
      <Card>
        <CardContent className="py-3 flex flex-wrap items-end gap-2">
          {([
            ['plate', 'Plate *', 'MH12DE1433'],
            ['owner', 'Owner', 'A. Sharma'],
            ['unit', 'Flat / unit', 'B-402'],
            ['type', 'Type', 'car / truck'],
            ['note', 'Note', ''],
          ] as const).map(([k, label, ph]) => (
            <label key={k} className="text-xs text-[var(--text-dim)]">
              {label}
              <input
                value={draft[k] ?? ''}
                onChange={(e) => setDraft((d) => ({ ...d, [k]: e.target.value }))}
                onKeyDown={(e) => { if (e.key === 'Enter') addDraft() }}
                placeholder={ph}
                className="block mt-0.5 py-1.5 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm text-[var(--text)] w-36"
              />
            </label>
          ))}
          <Button onClick={addDraft} disabled={saving || !normalizePlate(draft.plate)}>
            <Plus size={14} /> Add vehicle
          </Button>
          <div className="ml-auto flex items-center gap-2">
            <input
              ref={fileRef}
              type="file"
              accept=".csv,text/csv"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) importCsv(f)
                e.target.value = ''
              }}
            />
            <Button variant="outline" onClick={() => fileRef.current?.click()} disabled={saving}>
              <Upload size={14} /> Import CSV
            </Button>
            <Button variant="outline" onClick={exportCsv} disabled={!registry.length}>
              <Download size={14} /> Export CSV
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* The register */}
      {registry.length === 0 ? (
        <EmptyState
          icon={<Car size={28} />}
          title="No vehicles registered yet"
          description="Add each resident vehicle above, or import the whole list from a CSV (columns: plate, owner, unit, type, note). Then turn on the unknown-vehicle alarm and any stranger raises an alert."
        />
      ) : (
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-[var(--text-dim)] border-b border-[var(--border)]">
                  <th className="px-3 py-2">Plate</th>
                  <th className="px-3 py-2">Owner</th>
                  <th className="px-3 py-2">Flat / unit</th>
                  <th className="px-3 py-2">Type</th>
                  <th className="px-3 py-2">Note</th>
                  <th className="px-3 py-2 text-right pr-4" />
                </tr>
              </thead>
              <tbody>
                {[...registry].sort((a, b) => a.plate.localeCompare(b.plate)).map((r) => (
                  <tr key={r.plate} className="border-b border-[var(--border)] last:border-0 hover:bg-[var(--bg-2)]">
                    <td className="px-3 py-1.5 font-mono font-semibold">{r.plate}</td>
                    <td className="px-3 py-1.5">{r.owner || '—'}</td>
                    <td className="px-3 py-1.5">{r.unit || '—'}</td>
                    <td className="px-3 py-1.5">{r.type || '—'}</td>
                    <td className="px-3 py-1.5 text-[var(--text-dim)]">{r.note || ''}</td>
                    <td className="px-3 py-1.5 text-right pr-4">
                      <button
                        title="Remove from the register"
                        className="text-[var(--text-dim)] hover:text-[var(--danger,#e5484d)]"
                        onClick={() => onSaveRegistry(registry.filter((x) => x.plate !== r.plate))}
                        disabled={saving}
                      >
                        <Trash2 size={15} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
