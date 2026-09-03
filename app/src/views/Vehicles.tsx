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

import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BellRing, Car, Download, FileText, History, PhoneCall, Plus, RefreshCw,
  Search, ShieldAlert, ShieldCheck, Trash2, Upload,
} from 'lucide-react'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { AuthedImage } from '../components/AuthedImage'
import { Modal } from '../components/Modal'
import { useSnackbar } from '../components/Snackbar'
import { useInView } from '../hooks/useInView'
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
  // TRUE means there are two distinct images to show: the crop the plate
  // was READ from (#382) as well as the vehicle-best frame above. The
  // server sets it false when the two paths are equal, i.e. when there is
  // no second image to offer (timeline_events.py). The flags are
  // independent — a row can carry a crop and no frame.
  has_plate_evidence?: boolean
  plate_evidence_url?: string | null
  // The whole frame behind the vehicle crop. Independent of the two
  // flags above — the server sets it false when the scene and the crop
  // content-address to the same file, i.e. when there is nothing wider
  // to show. NULL/false on every read stored before Tier-0 sent scenes.
  has_scene_evidence?: boolean
  scene_evidence_url?: string | null
  // The frame the plate crop was cut from. The only image on the row
  // guaranteed to show the car the number belongs to: a visit is not
  // always one vehicle (track association merges a departing car with
  // the one behind it), so the vehicle frame and the scene can both be
  // a different car from the plate.
  has_plate_frame?: boolean
  plate_frame_url?: string | null
  payload?: { plate_merged?: boolean } | null
}

// The two images are different frames by construction — multi-frame OCR
// reads plate candidates, while the best frame is picked for the biggest
// VEHICLE box, which at close range is exactly when the plate leaves the
// crop. The preview dialog stacks BOTH, so it never has to choose (#387);
// the row thumbnail still does, having room for one, and picks the
// vehicle — the number is already spelled out in the next column.
//
// The queryKey shapes are shared with the thumbnail on purpose: opening
// the dialog re-uses whichever blob the row already fetched, so it pays
// for one request, not two.

/** The vehicle-best frame — context: which car. */
function vehicleFrameSource(e: PlateEvent) {
  return {
    queryKey: ['evidence', e.id],
    fetchBlob: () => apiService.getEventEvidence(e.id),
  }
}

/** The crop the plate was READ from. Only meaningful when
 *  ``has_plate_evidence``: otherwise /plate-evidence 404s, or answers the
 *  vehicle frame's own bytes. */
function plateCropSource(e: PlateEvent) {
  return {
    queryKey: ['plate-evidence', e.id],
    fetchBlob: () => apiService.getEventPlateEvidence(e.id),
  }
}

/** The whole camera frame — context: what the camera actually saw. Only
 *  meaningful when ``has_scene_evidence``. */
function sceneFrameSource(e: PlateEvent) {
  return {
    queryKey: ['scene-evidence', e.id],
    fetchBlob: () => apiService.getEventSceneEvidence(e.id),
  }
}

/** The frame the plate crop came from — proof: WHICH car. */
function plateFrameSource(e: PlateEvent) {
  return {
    queryKey: ['plate-frame', e.id],
    fetchBlob: () => apiService.getEventPlateFrame(e.id),
  }
}

// One image for a 64px row thumbnail: the vehicle, not the number. The
// plate crop reads as a number the row already prints in text, so it
// spent a whole column saying nothing new — the operator wants to see
// WHICH car.
//
// Prefer the frame the plate was READ from. It is the only wide image
// guaranteed to hold the car the number belongs to: /evidence and
// /scene-evidence are the visit's best-thumbnail moment, and a merged
// track puts one car's plate on another car's best frame. Falling back
// to those is still right for rows stored before the column existed —
// a possibly-wrong car beats an empty box, and the dialog shows the
// plate crop alongside for anyone checking.
function rowThumbImage(e: PlateEvent) {
  if (e.has_plate_frame) return plateFrameSource(e)
  if (e.has_scene_evidence) return sceneFrameSource(e)
  return vehicleFrameSource(e)
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

type VehicleReport = {
  year: number
  month: number
  total_reads: number
  unique_plates: number
  per_camera: { camera_id: number; reads: number }[]
  per_plate: {
    plate: string
    reads: number
    first_seen: string | null
    last_seen: string | null
    per_camera: { camera_id: number; reads: number }[]
  }[]
  per_day: { day: string; reads: number }[]
}

/** The enabled app providing LPR — capability-keyed (community-proof). */
export function findLprApp(apps: RegisteredApp[] | undefined): RegisteredApp | null {
  return (
    (apps ?? []).find(
      (a) => a.enabled && (a.manifest?.requires_tasks ?? []).includes(LPR_TASK)
    ) ?? null
  )
}

/** One row of the society's vehicle register (app config `registry`).
 * `expires` (YYYY-MM-DD) makes an entry a visitor pass — past the date
 * the app treats the plate as a stranger again. */
export type RegistryEntry = {
  plate: string
  owner?: string
  unit?: string
  type?: string
  model?: string
  note?: string
  expires?: string
}

const REGISTRY_FIELDS = ['owner', 'unit', 'type', 'model', 'note', 'expires'] as const

/** Is a register entry currently valid (visitor pass not expired)? */
export function entryActive(e: RegistryEntry, today = new Date()): boolean {
  if (!e.expires) return true
  const d = new Date(`${e.expires}T23:59:59`)
  return Number.isNaN(d.getTime()) ? true : today <= d
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
      for (const k of REGISTRY_FIELDS) {
        const v = String((e as any)[k] ?? '').trim()
        if (v) r[k] = v
      }
      out.push(r)
    }
  }
  return out
}

// Society spreadsheets rarely use our exact column names — match the
// obvious variants ("Vehicle No", "Flat No", "Car Model", …) so a
// secretary's existing sheet imports as-is.
const HEADER_SYNONYMS: Record<string, (typeof REGISTRY_FIELDS)[number] | 'plate'> = {
  plate: 'plate', 'plate no': 'plate', 'plate number': 'plate',
  'number plate': 'plate', number: 'plate', 'vehicle no': 'plate',
  'vehicle number': 'plate', 'reg no': 'plate', 'registration no': 'plate',
  'registration number': 'plate', 'car no': 'plate', 'car number': 'plate',
  owner: 'owner', 'owner name': 'owner', name: 'owner', resident: 'owner',
  unit: 'unit', flat: 'unit', 'flat no': 'unit', 'flat number': 'unit',
  house: 'unit', 'house no': 'unit', apartment: 'unit', wing: 'unit',
  type: 'type', 'vehicle type': 'type', category: 'type',
  model: 'model', 'vehicle model': 'model', 'car model': 'model',
  make: 'model', 'make and model': 'model', 'make/model': 'model',
  note: 'note', notes: 'note', remark: 'note', remarks: 'note',
  expires: 'expires', expiry: 'expires', 'expiry date': 'expires',
  'valid till': 'expires', 'valid until': 'expires', 'valid upto': 'expires',
}

/** Rows (first row possibly a header) → registry entries. Shared by
 * the CSV and Excel importers. Without a recognisable header, the
 * first column is the plate and the second the owner. */
export function registryFromRows(rows: (string | number | null | undefined)[][]): RegistryEntry[] {
  const clean = rows
    .map((r) => r.map((c) => String(c ?? '').trim()))
    .filter((r) => r.some(Boolean))
  if (!clean.length) return []

  const header = clean[0].map((h) => HEADER_SYNONYMS[h.toLowerCase()] ?? null)
  const hasHeader = header.includes('plate')
  const cols: (string | null)[] = hasHeader
    ? header
    : ['plate', 'owner', 'unit', 'type', 'model', 'note', 'expires']
  const body = hasHeader ? clean.slice(1) : clean

  const out: RegistryEntry[] = []
  for (const r of body) {
    const rec: Record<string, string> = {}
    r.forEach((v, i) => {
      const k = cols[i]
      if (k && v) rec[k] = v
    })
    const plate = normalizePlate(rec.plate ?? '')
    if (!plate) continue
    const entry: RegistryEntry = { plate }
    for (const k of REGISTRY_FIELDS) {
      if (rec[k]) entry[k] = rec[k]
    }
    out.push(entry)
  }
  return out
}

/** Minimal CSV parse (quoted fields supported) → registry entries via
 * the shared row parser (header synonyms and all). */
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
  return registryFromRows(rows)
}

/** Excel (.xlsx/.xls) → registry entries. SheetJS is lazy-loaded so
 * the page's normal bundle doesn't carry it; the first sheet's rows
 * go through the same header matching as CSV. */
export async function registryFromExcel(buf: ArrayBuffer): Promise<RegistryEntry[]> {
  const XLSX = await import('xlsx')
  // cellDates + dateNF: a date-TYPED "Valid Till" cell must land as
  // YYYY-MM-DD (what the app's expiry check parses), not the sheet's
  // locale format like 9/5/26 — which would silently never expire.
  const wb = XLSX.read(buf, { type: 'array', cellDates: true })
  const sheet = wb.Sheets[wb.SheetNames[0]]
  if (!sheet) return []
  const rows = XLSX.utils.sheet_to_json(sheet, {
    header: 1,
    raw: false,   // numbers come back as displayed strings
    dateNF: 'yyyy-mm-dd',
    defval: '',
  }) as (string | number | null)[][]
  return registryFromRows(rows)
}

function registryToCsv(entries: RegistryEntry[]): string {
  const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
  return [
    'plate,owner,unit,type,model,note,expires',
    ...entries.map((e) =>
      [e.plate, e.owner, e.unit, e.type, e.model, e.note, e.expires].map(esc).join(',')),
  ].join('\n')
}

// ── Camera roles ────────────────────────────────────────────────────
// Every camera can be given a place in the vehicle story: a Gate IN
// camera is what makes the vertical work (entries, unknown alarms at
// the gate); Gate OUT unlocks stays and "inside now"; Parking and
// custom-named locations enrich the history. Stored in the app's
// config as camera_roles: {cameraId: {role, label?}}.

export type CameraRole = 'gate_in' | 'gate_out' | 'parking' | 'other'
export type CameraRoleEntry = { role: CameraRole; label?: string }

export function parseCameraRoles(cfg: any): Record<string, CameraRoleEntry> {
  const out: Record<string, CameraRoleEntry> = {}
  const roles = cfg?.camera_roles
  if (roles && typeof roles === 'object') {
    for (const [id, v] of Object.entries(roles as Record<string, any>)) {
      const role = String((v as any)?.role ?? v ?? '')
      if (role === 'gate_in' || role === 'gate_out' || role === 'parking' || role === 'other') {
        const label = String((v as any)?.label ?? '').trim()
        out[id] = label ? { role: role as CameraRole, label } : { role: role as CameraRole }
      }
    }
  }
  // Back-compat: the earlier gate_directions map ('in'/'out').
  const legacy = cfg?.gate_directions
  if (legacy && typeof legacy === 'object') {
    for (const [id, d] of Object.entries(legacy as Record<string, string>)) {
      if (out[id]) continue
      if (d === 'in') out[id] = { role: 'gate_in' }
      else if (d === 'out') out[id] = { role: 'gate_out' }
    }
  }
  return out
}

export function roleLabel(entry: CameraRoleEntry | undefined): string | null {
  if (!entry) return null
  if (entry.role === 'gate_in') return 'IN'
  if (entry.role === 'gate_out') return 'OUT'
  if (entry.role === 'parking') return 'Parking'
  return entry.label || 'Other'
}

// ── Plate monitors ──────────────────────────────────────────────────
// A monitor is one plate under surveillance with its own alert
// configuration; the legacy denylist reads as active high monitors.

export type Monitor = {
  plate: string
  note?: string
  severity?: 'info' | 'low' | 'medium' | 'high' | 'critical'
  active?: boolean
  cameras?: string[]
}

export const MONITOR_SEVERITIES = ['info', 'low', 'medium', 'high', 'critical'] as const

export function parseMonitors(cfg: any): Monitor[] {
  const out = new Map<string, Monitor>()
  for (const e of Array.isArray(cfg?.monitors) ? cfg.monitors : []) {
    if (typeof e === 'string') {
      const plate = normalizePlate(e)
      if (plate) out.set(plate, { plate, severity: 'high', active: true })
      continue
    }
    if (!e || typeof e !== 'object') continue
    const plate = normalizePlate(String((e as any).plate ?? ''))
    if (!plate) continue
    const sev = String((e as any).severity ?? 'high') as Monitor['severity']
    out.set(plate, {
      plate,
      note: String((e as any).note ?? '').trim() || undefined,
      severity: (MONITOR_SEVERITIES as readonly string[]).includes(sev as string) ? sev : 'high',
      active: (e as any).active !== false,
      cameras: Array.isArray((e as any).cameras)
        ? (e as any).cameras.map((c: any) => String(c)).filter(Boolean)
        : undefined,
    })
  }
  // Denylist shorthand — never overriding an explicit monitor.
  for (const p of Array.isArray(cfg?.denylist) ? cfg.denylist : []) {
    const plate = normalizePlate(String(p))
    if (plate && !out.has(plate)) out.set(plate, { plate, severity: 'high', active: true })
  }
  return [...out.values()]
}

function formatStay(seconds: number): string {
  const m = Math.round(seconds / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
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
  // The input stays instant; the QUERY waits. Without this every keystroke
  // swapped the events queryKey, and since there is no keepPreviousData the
  // table blanked and re-rendered its rows — a six-character plate meant six
  // rounds of image mounts. Same 250ms the camera search uses.
  const [debouncedPlate, setDebouncedPlate] = useState('')
  useEffect(() => {
    const t = setTimeout(() => setDebouncedPlate(plate), 250)
    return () => clearTimeout(t)
  }, [plate])
  const [cameraId, setCameraId] = useState<number | ''>('')
  const [range, setRange] = useState<(typeof RANGE_PRESETS)[number]>(RANGE_PRESETS[0])
  const [preview, setPreview] = useState<PlateEvent | null>(null)
  const [tab, setTab] = useState<'reads' | 'registry' | 'monitoring'>('reads')
  const [historyPlate, setHistoryPlate] = useState<string | null>(null)
  const [reportOpen, setReportOpen] = useState(false)
  const [registerPrefill, setRegisterPrefill] = useState('')

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
    queryKey: ['plate-events', debouncedPlate, cameraId, range.key],
    queryFn: async () => {
      const { data } = await apiService.getPlateEvents({
        plate: debouncedPlate.trim() || undefined,
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

  // The society register + alarm mode live in the providing app's
  // config, exactly like the watchlists (live-applied by the app).
  const registry = useMemo(
    () => parseRegistry((lprApp?.config as any)?.registry),
    [lprApp]
  )
  // Expired visitor passes are strangers again — the badges track that.
  const registryPlates = useMemo(
    () => new Set(registry.filter((r) => entryActive(r)).map((r) => r.plate)),
    [registry]
  )
  const expiredPlates = useMemo(
    () => new Set(registry.filter((r) => !entryActive(r)).map((r) => r.plate)),
    [registry]
  )
  const alarmOnUnknown = Boolean((lprApp?.config as any)?.alarm_on_unknown)
  const barrierMode: 'off' | 'registered' =
    (lprApp?.config as any)?.barrier_mode === 'registered' ? 'registered' : 'off'

  // Camera roles — which camera is the entry gate, the exit, parking,
  // or another named location — are the vertical's settings, so they
  // live in the app's config too (keys are camera ids as strings).
  const cameraRoles = useMemo(
    () => parseCameraRoles(lprApp?.config),
    [lprApp]
  )
  const inCams = useMemo(
    () => Object.entries(cameraRoles).filter(([, r]) => r.role === 'gate_in').map(([id]) => Number(id)),
    [cameraRoles]
  )
  const outCams = useMemo(
    () => Object.entries(cameraRoles).filter(([, r]) => r.role === 'gate_out').map(([id]) => Number(id)),
    [cameraRoles]
  )
  const gatesConfigured = inCams.length > 0 && outCams.length > 0

  // Monitors (surveillance list) — merged view incl. legacy denylist.
  const monitors = useMemo(() => parseMonitors(lprApp?.config), [lprApp])
  const monitoredPlates = useMemo(
    () => new Set(monitors.map((m) => m.plate)),
    [monitors]
  )

  const occupancyQuery = useQuery({
    queryKey: ['gate-occupancy', inCams.join('.'), outCams.join('.')],
    queryFn: async () => {
      const { data } = await apiService.getGateOccupancy(inCams, outCams)
      return data as { inside: number; plates: string[] }
    },
    enabled: gatesConfigured,
    retry: 0,
    refetchInterval: 60_000,
  })

  const sessionsQuery = useQuery({
    queryKey: ['plate-sessions', historyPlate, inCams.join('.'), outCams.join('.')],
    queryFn: async () => {
      const { data } = await apiService.getPlateSessions(historyPlate as string, inCams, outCams)
      return data as {
        sessions: {
          entered_at: string | null
          entry_camera_id: number | null
          exited_at: string | null
          exit_camera_id: number | null
          duration_seconds: number | null
        }[]
        inside_now: boolean
      }
    },
    enabled: Boolean(historyPlate) && gatesConfigured,
    retry: 0,
  })

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

  // The providing app's live state: the review queue (reads a human
  // should look at) and the inside-visitors count for overstay.
  const appStateQuery = useQuery({
    queryKey: ['app-status', lprApp?.id],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(lprApp!.id)
      return data as { state?: {
        review?: { plate: string; camera_id: string; confidence?: number | null; reason: string; time: number }[]
        inside_visitors?: number
      } }
    },
    enabled: Boolean(lprApp),
    retry: 0,
    refetchInterval: 15_000,
  })
  const reviewQueue = appStateQuery.data?.state?.review ?? []

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

  // Quick actions off the reads table: allowlist stays a plain list;
  // "monitor this plate" writes a monitor rule (the denylist's
  // successor — same live-update path, per-plate alert config).
  const watchlist = useMutation({
    mutationFn: async ({ plateText, list }: { plateText: string; list: 'allowlist' | 'monitor' }) => {
      if (!lprApp) throw new Error('No enabled LPR app to hold the watchlist.')
      const cfg = { ...(lprApp.config ?? {}) } as Record<string, any>
      if (list === 'allowlist') {
        const current: string[] = Array.isArray(cfg.allowlist) ? cfg.allowlist : []
        if (current.includes(plateText)) return
        cfg.allowlist = [...current, plateText]
      } else {
        if (monitoredPlates.has(plateText)) return
        cfg.monitors = [
          ...monitors,
          { plate: plateText, severity: 'high', active: true },
        ]
      }
      await apiService.updateAppConfig(lprApp.id, cfg)
    },
    onSuccess: (_d, vars) => {
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      showSuccess(vars.list === 'monitor'
        ? `${vars.plateText} is now monitored — configure its alert in the Monitoring tab`
        : `${vars.plateText} added to the allowlist`)
    },
    onError: (e) => showError(extractApiError(e, 'Could not update the watchlist.')),
  })

  // Replace the whole monitors list (Monitoring tab edits). The tab
  // edits the MERGED view (explicit monitors + denylist shorthand), so
  // every save migrates the legacy denylist into explicit monitors and
  // clears it — deletions included.
  const saveMonitors = (next: Monitor[]) => {
    saveConfig.mutate({ monitors: next, denylist: [] })
  }

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
            <Button variant="outline" onClick={() => setReportOpen(true)}>
              <FileText size={14} /> Monthly report
            </Button>
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
          gatesConfigured
            ? { label: 'Inside now', value: occupancyQuery.data?.inside }
            : { label: 'Busiest camera (7d)', value: stats?.per_camera?.length
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
          { key: 'monitoring', label: `Monitoring (${monitors.length})` },
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

      {tab === 'monitoring' ? (
        <MonitoringTab
          monitors={monitors}
          canEdit={Boolean(lprApp)}
          saving={saveConfig.isPending}
          cameras={camerasQuery.data ?? []}
          onSave={saveMonitors}
        />
      ) : tab === 'registry' ? (
        <RegistryTab
          registry={registry}
          alarmOnUnknown={alarmOnUnknown}
          barrierMode={barrierMode}
          overstayHours={Number((lprApp?.config as any)?.overstay_hours ?? 0) || 0}
          onSetOverstay={(hours) => {
            saveConfig.mutate({ overstay_hours: hours }, {
              onSuccess: () => showSuccess(hours > 0
                ? `Overstay alert ON — visitors inside longer than ${hours}h raise an alert`
                : 'Overstay alert off'),
            })
          }}
          onToggleBarrier={(on) => {
            saveConfig.mutate({ barrier_mode: on ? 'registered' : 'off' }, {
              onSuccess: () => showSuccess(
                on ? 'Automatic barrier ON — allow/deny decisions now publish for every gate-IN read'
                   : 'Automatic barrier off'),
            })
          }}
          canEdit={Boolean(lprApp)}
          saving={saveConfig.isPending}
          cameras={camerasQuery.data ?? []}
          cameraRoles={cameraRoles}
          onSetRole={(cameraId, role, label) => {
            const next = { ...cameraRoles }
            if (!role) delete next[String(cameraId)]
            else next[String(cameraId)] = label ? { role, label } : { role }
            // Only declared manifest params may be written — the server
            // rejects unknown config keys. camera_roles is declared;
            // the legacy gate_directions map is read-only fallback.
            saveConfig.mutate({ camera_roles: next })
          }}
          initialPlate={registerPrefill}
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
      {/* ── Needs review (bad format / low confidence reads) ──────── */}
      {reviewQueue.length > 0 && (
        <Card>
          <CardContent className="py-3">
            <div className="text-sm font-medium mb-1 text-[var(--warning,#b7791f)]">
              Reads needing review ({reviewQueue.length})
            </div>
            <div className="text-xs text-[var(--text-dim)] mb-2">
              The app did not act on these — bad plate format or OCR confidence
              below your threshold. A human decides: register the plate if it's
              real, or ignore it (entries age out on their own).
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <tbody>
                  {[...reviewQueue].reverse().slice(0, 8).map((r, i) => (
                    <tr key={`${r.plate}-${r.time}-${i}`} className="border-b border-[var(--border)] last:border-0">
                      <td className="py-1 pr-3 font-mono font-semibold">{r.plate}</td>
                      <td className="py-1 pr-3 text-[var(--text-dim)]">{cameraName(Number(String(r.camera_id).replace(/^cam/, '')) || 0)}</td>
                      <td className="py-1 pr-3">
                        <Badge variant="warning">
                          {r.reason === 'low_confidence'
                            ? `low confidence${r.confidence != null ? ` (${Math.round(r.confidence * 100)}%)` : ''}`
                            : 'bad format'}
                        </Badge>
                      </td>
                      <td className="py-1 pr-3 text-[var(--text-dim)]">
                        {new Date(r.time * 1000).toLocaleTimeString()}
                      </td>
                      <td className="py-1 text-right">
                        <Button
                          variant="outline"
                          onClick={() => {
                            setRegisterPrefill(r.plate)
                            setTab('registry')
                          }}
                        >
                          <Plus size={13} /> Register
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

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
                  const inDeny = monitoredPlates.has(p)
                  const inAllow = allow.includes(p)
                  const registered = registryPlates.has(p)
                  return (
                    <tr key={e.id} className="border-b border-[var(--border)] last:border-0 hover:bg-[var(--bg-2)]">
                      <td className="px-3 py-1.5">
                        {(e.has_plate_evidence || e.has_evidence) ? (
                          <RowThumb e={e} plate={p} onOpen={() => setPreview(e)} />
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
                      <td className="px-3 py-1.5">
                        {cameraName(e.camera_id)}
                        {(() => {
                          const r = cameraRoles[String(e.camera_id)]
                          const label = roleLabel(r)
                          if (!label) return null
                          return (
                            <Badge
                              variant={r!.role === 'gate_in' ? 'success' : 'neutral'}
                              className="ml-1.5"
                            >
                              {label}
                            </Badge>
                          )
                        })()}
                      </td>
                      <td className="px-3 py-1.5 text-[var(--text-dim)]">
                        {e.started_at ? new Date(e.started_at).toLocaleString() : '—'}
                      </td>
                      <td className="px-3 py-1.5">
                        {inDeny ? <Badge variant="destructive" title={monitors.find((m) => m.plate === p)?.note}>monitored</Badge>
                          : registered ? <Badge variant="success">registered</Badge>
                          : expiredPlates.has(p) ? <Badge variant="warning">pass expired</Badge>
                          : inAllow ? <Badge variant="success">expected</Badge>
                          : alarmOnUnknown ? <Badge variant="warning">unknown</Badge>
                          : <Badge variant="neutral">{e.label || 'vehicle'}</Badge>}
                      </td>
                      <td className="px-3 py-1.5 text-right pr-4 whitespace-nowrap">
                        {lprApp && !inDeny && (
                          <button
                            title="Monitor this plate — alert whenever it is seen (configure in the Monitoring tab)"
                            className="text-[var(--text-dim)] hover:text-[var(--text)] mr-2"
                            onClick={() => watchlist.mutate({ plateText: p, list: 'monitor' })}
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

      {/* ── Custom solutions (the providing app's contact) ────────── */}
      {(() => {
        const contact =
          (lprApp?.manifest as any)?.contact || (lprApp?.manifest as any)?.website
        if (!contact) return null
        return (
          <Card>
            <CardContent className="py-3 flex flex-wrap items-center gap-3">
              <PhoneCall size={18} className="text-[var(--text-dim)] shrink-0" />
              <div className="min-w-0 flex-1 text-sm">
                <span className="font-medium">Need more for your site?</span>{' '}
                <span className="text-[var(--text-dim)]">
                  Housing societies, industrial estates, factories, company campuses,
                  warehouses and logistics yards — phone-number &amp; SMS alerts, WhatsApp
                  notifications, complete gate &amp; process automation (barrier lift for
                  registered vehicles, truck-bay logging), scheduled reports, or any
                  custom feature. We build per-site solutions.
                </span>
              </div>
              <Button
                variant="outline"
                onClick={() => window.open(contact, '_blank', 'noopener,noreferrer')}
              >
                Contact us
              </Button>
            </CardContent>
          </Card>
        )
      })()}

      {/* ── Printable monthly report ──────────────────────────────── */}
      {reportOpen && (
        <ReportOverlay
          registry={registry}
          monitors={monitors}
          cameraRoles={cameraRoles}
          cameraName={cameraName}
          onClose={() => setReportOpen(false)}
        />
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
                return reg && (reg.owner || reg.unit || reg.type || reg.model || reg.note) ? (
                  <div className="rounded border border-[var(--border)] bg-[var(--bg-2)] px-3 py-2">
                    Registered{reg.owner ? ` to ${reg.owner}` : ''}
                    {reg.unit ? ` · ${reg.unit}` : ''}
                    {reg.model ? ` · ${reg.model}` : reg.type ? ` · ${reg.type}` : ''}
                    {reg.expires ? ` · valid till ${reg.expires}` : ''}
                    {reg.note ? ` — ${reg.note}` : ''}
                    {!entryActive(reg) && <Badge variant="warning" className="ml-2">expired</Badge>}
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
              {gatesConfigured && sessionsQuery.data && (
                <div>
                  <div className="text-xs text-[var(--text-dim)] mb-1 flex items-center gap-2">
                    Gate in / gate out
                    {sessionsQuery.data.inside_now && (
                      <Badge variant="success">inside now</Badge>
                    )}
                  </div>
                  {sessionsQuery.data.sessions.length === 0 ? (
                    <div className="text-xs text-[var(--text-dim)]">No gate passages yet.</div>
                  ) : (
                    <div className="max-h-48 overflow-y-auto">
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="text-left text-[var(--text-dim)]">
                            <th className="py-1 pr-2">In</th>
                            <th className="py-1 pr-2">Out</th>
                            <th className="py-1 text-right">Stay</th>
                          </tr>
                        </thead>
                        <tbody>
                          {sessionsQuery.data.sessions.map((s, i) => (
                            <tr key={i} className="border-t border-[var(--border)]">
                              <td className="py-1 pr-2">
                                {s.entered_at
                                  ? `${new Date(s.entered_at).toLocaleString()} · ${cameraName(s.entry_camera_id as number)}`
                                  : <span className="text-[var(--text-dim)]">missed</span>}
                              </td>
                              <td className="py-1 pr-2">
                                {s.exited_at
                                  ? `${new Date(s.exited_at).toLocaleString()} · ${cameraName(s.exit_camera_id as number)}`
                                  : <span className="text-[var(--text-dim)]">{i === 0 && sessionsQuery.data!.inside_now ? 'still inside' : 'missed'}</span>}
                              </td>
                              <td className="py-1 text-right font-mono">
                                {s.duration_seconds != null ? formatStay(s.duration_seconds) : '—'}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}
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

      {/* ── Evidence preview ─────────────────────── */}
      {/* Keyed by id so the stage/zoom toggles reset with the row, and
          extracted so that state lives with the dialog rather than in
          this 600-line component. */}
      {preview && (
        <EvidenceDialog
          key={preview.id}
          e={preview}
          cameraLabel={cameraName(preview.camera_id)}
          onClose={() => setPreview(null)}
        />
      )}
    </section>
  )
}

/** The Photo cell: reserves its box, fetches only once on screen.
 *
 *  An auth-gated image cannot use <img loading="lazy"> — it has to come
 *  through the api client as a blob — so before this every rendered row
 *  fired a request the instant the table painted. At 200 rows over HTTP/2
 *  that is ~200 at once, each holding a server-side DB connection while it
 *  runs, which is precisely what exhausted core's pool.
 *
 *  The placeholder is the SAME h-10 w-16 box as the image: anything else
 *  and rows resize as they scroll in, which moves the very elements the
 *  observer is measuring.
 */
function RowThumb({ e, plate, onOpen }: {
  e: PlateEvent
  plate: string
  onOpen: () => void
}) {
  const [ref, seen] = useInView<HTMLDivElement>()
  return (
    <div ref={ref} className="h-10 w-16">
      {seen ? (
        <AuthedImage
          {...rowThumbImage(e)}
          alt={`vehicle for ${plate}`}
          // A camera frame is 16:9 against a 1.6:1 cell — close enough
          // that object-cover trims the sides rather than letterboxing a
          // already-small picture. Nothing here has to stay legible the
          // way the plate crop did (#385); it is a "which car" glance,
          // and the dialog is one click away.
          className="h-10 w-16 rounded cursor-zoom-in object-cover"
          onClick={onOpen}
        />
      ) : (
        <div className="h-10 w-16 rounded bg-[var(--bg-2)]" />
      )}
    </div>
  )
}

// ── Evidence preview ───────────────────────────────────

/** One read, shown the way an operator reads it: the scene first.
 *
 *  The stage is the WHOLE camera frame (#387 follow-up) — /evidence is a
 *  crop of the detection box plus a quarter-box margin, which keeps the
 *  subject dominant and is exactly why it cannot answer "what lane, whose
 *  gate, next to what". The plate crop is the proof the number was read
 *  off that moment, so it rides ON the scene as a card rather than
 *  competing with it for the 85vh budget the stacked layout fought over.
 *
 *  Older reads carry no scene (the bytes were never stored, so there is
 *  nothing to backfill): the stage falls back to the vehicle crop and the
 *  Scene/Vehicle toggle is not offered.
 */
function EvidenceDialog({
  e, cameraLabel, onClose,
}: {
  e: PlateEvent
  cameraLabel: string
  onClose: () => void
}) {
  const plate = (e.plate_text ?? '').toUpperCase()
  const hasScene = !!e.has_scene_evidence
  const hasRead = !!e.has_plate_frame
  const hasFrame = hasRead || hasScene || !!e.has_evidence
  // Default to the frame the plate was READ from, because it is the
  // only one that cannot be a different car. Scene is the wide look;
  // the vehicle crop is just a tighter cut of the same moment as the
  // scene, so it earns no button of its own — it is only ever the
  // fallback for rows that predate the other two.
  const [stage, setStage] = useState<'read' | 'scene'>(hasRead ? 'read' : 'scene')
  // Hover handles the mouse; this handles touch (where hover does not
  // exist) and the keyboard, which is why the card is a real <button>.
  const [zoomed, setZoomed] = useState(false)
  const showRead = hasRead && stage === 'read'
  const showScene = !showRead && hasScene
  const seen = e.started_at ? new Date(e.started_at).toLocaleString() : null
  const stageSource = showRead
    ? plateFrameSource(e)
    : showScene ? sceneFrameSource(e) : vehicleFrameSource(e)

  const caveats = [
    e.payload?.plate_merged &&
      'read reconstructed from more than one frame — the crop shown is the clearer of them',
    // The wide shot is the visit's best-thumbnail moment, which is not
    // always the same vehicle the plate came off. Say so rather than
    // letting the operator assume the big picture is the match.
    showScene && hasRead &&
      'the scene is the visit’s best frame — switch to Read frame for the car this number came off',
    // Says WHY an old read looks different from a new one, which is
    // otherwise indistinguishable from the feature being broken.
    !hasRead && !hasScene && e.has_evidence &&
      'no full frame stored for this read — showing the vehicle crop',
    !e.has_plate_evidence && e.has_evidence &&
      'no separate plate crop stored',
  ].filter(Boolean) as string[]

  // Anchored top-left so the card grows OVER the scene without moving:
  // a centred transform would slide the plate out from under the cursor.
  // 176px x 2.2 = ~390px, comfortably inside the stage, and the figure's
  // overflow-hidden clips it rather than bursting the dialog on a short
  // viewport.
  const card = (
    <button
      type="button"
      onClick={() => setZoomed((z) => !z)}
      aria-pressed={zoomed}
      aria-label={zoomed ? 'Shrink plate crop' : 'Enlarge plate crop'}
      className={`absolute top-2 left-2 z-20 w-[176px] origin-top-left text-left
        border border-white/20 bg-black/65 backdrop-blur-sm shadow-lg
        transition-transform duration-200 ease-out ${
          zoomed ? 'scale-[2.2]' : 'hover:scale-[2.2] focus-visible:scale-[2.2]'
        }`}
    >
      {e.has_plate_evidence && (
        // Size the SLOT, never the <img>: AuthedImage puts its className
        // on the loading pulse and the "no photo" tile too, so a
        // height-less class collapses both to 0px and the card jumps
        // when the blob lands.
        <div className="w-full aspect-[26/10] bg-black/40">
          <AuthedImage
            {...plateCropSource(e)}
            alt={`plate crop for ${plate}`}
            className="h-full w-full object-contain"
          />
        </div>
      )}
      <div className="px-2 py-1 border-t border-white/10">
        <div className="text-[11px] font-semibold leading-4 tracking-wide text-white">
          {plate || 'plate not read'}
        </div>
        {/* The read time lives here, on the image, not in a footnote: it
            is the second thing anyone checks after the number itself. */}
        <div className="text-[10px] leading-4 text-white/70">
          {seen ? `Seen ${seen}` : 'Read time not recorded'}
        </div>
      </div>
    </button>
  )

  return (
    <Modal
      open
      onClose={onClose}
      title={`${plate} — ${cameraLabel}`}
      // The default w-[720px] is a FIXED width that overflows a phone;
      // same inset-fit pattern as AddCameraDialog.
      widthClassName="w-full max-w-[860px] mx-4"
    >
      {hasFrame ? (
        <figure className="relative border border-[var(--border)] bg-[var(--bg-2)] overflow-hidden">
          {/* 16/9 is nominal — object-contain letterboxes anything else
              rather than distorting it — and max-h keeps the whole dialog
              inside Modal's 85vh without the two-slot arithmetic the
              stacked layout needed. */}
          <div className="w-full aspect-[16/9] min-h-[180px] max-h-[calc(85vh_-_150px)]">
            <AuthedImage
              {...stageSource}
              alt={
                showRead
                  ? `the frame ${plate} was read from`
                  : showScene
                    ? `camera frame for ${plate}`
                    : `vehicle frame for ${plate}`
              }
              className="h-full w-full object-contain"
            />
          </div>
          {card}
          {/* Only when there are genuinely two pictures to choose
              between. Proof vs context — not two crops of one moment. */}
          {hasRead && hasScene && (
            <div className="absolute top-2 right-2 z-20 flex border border-white/20 bg-black/65 backdrop-blur-sm text-[11px] leading-4">
              {(['read', 'scene'] as const).map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setStage(s)}
                  className={`px-2 py-1 ${
                    stage === s
                      ? 'bg-white/20 text-white'
                      : 'text-white/60 hover:text-white'
                  }`}
                >
                  {s === 'read' ? 'Read frame' : 'Scene'}
                </button>
              ))}
            </div>
          )}
        </figure>
      ) : (
        // No frame of any kind — a plate crop with nothing to pin it on.
        // The card needs a positioned parent, so give it a plain one.
        <div className="relative min-h-[120px] border border-[var(--border)] bg-[var(--bg-2)]">
          {card}
        </div>
      )}

      {/* Caveats only. The time moved onto the image, and an empty strip
          below a full-bleed photo reads as a rendering bug. */}
      {caveats.length > 0 && (
        <div className="text-xs text-[var(--text-dim)] mt-3 space-y-0.5">
          {caveats.map((c) => (
            <div key={c}>· {c}</div>
          ))}
        </div>
      )}
    </Modal>
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
  barrierMode,
  onToggleBarrier,
  overstayHours,
  onSetOverstay,
  initialPlate,
  canEdit,
  saving,
  cameras,
  cameraRoles,
  onSetRole,
  onSaveRegistry,
  onToggleAlarm,
}: {
  registry: RegistryEntry[]
  alarmOnUnknown: boolean
  barrierMode: 'off' | 'registered'
  onToggleBarrier: (on: boolean) => void
  overstayHours: number
  onSetOverstay: (hours: number) => void
  initialPlate?: string
  canEdit: boolean
  saving: boolean
  cameras: CameraRow[]
  cameraRoles: Record<string, CameraRoleEntry>
  onSetRole: (cameraId: number, role: CameraRole | '', label?: string) => void
  onSaveRegistry: (entries: RegistryEntry[]) => void
  onToggleAlarm: (on: boolean) => void
}) {
  const [draft, setDraft] = useState<RegistryEntry>({ plate: initialPlate ?? '' })
  const [overstayDraft, setOverstayDraft] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)
  const { showError, showSuccess } = useSnackbar()

  const addDraft = () => {
    const plate = normalizePlate(draft.plate)
    if (!plate) return
    const entry: RegistryEntry = { plate }
    for (const k of REGISTRY_FIELDS) {
      const v = (draft[k] ?? '').trim()
      if (v) entry[k] = v
    }
    onSaveRegistry([...registry.filter((r) => r.plate !== plate), entry])
    setDraft({ plate: '' })
  }

  // CSV or Excel — a society's list usually already exists as a sheet.
  const importFile = async (file: File) => {
    try {
      const isExcel = /\.xlsx?$/i.test(file.name)
      const imported = isExcel
        ? await registryFromExcel(await file.arrayBuffer())
        : registryFromCsv(await file.text())
      if (!imported.length) {
        showError('No plates found in that file — the plate column was not recognised.')
        return
      }
      // Imported rows win over existing ones with the same plate.
      const merged = new Map(registry.map((r) => [r.plate, r] as const))
      for (const e of imported) merged.set(e.plate, e)
      onSaveRegistry([...merged.values()])
      showSuccess(`Imported ${imported.length} vehicles from ${file.name}`)
    } catch (e: any) {
      showError(e?.message || 'Could not read that file.')
    }
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

      {/* Automatic barrier — the decision half of gate automation */}
      <Card>
        <CardContent className="py-3 flex flex-wrap items-center gap-3">
          <Car size={18} className={barrierMode === 'registered' ? 'text-[var(--success,#46a758)]' : 'text-[var(--text-dim)]'} />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium">Automatic barrier</div>
            <div className="text-xs text-[var(--text-dim)]">
              Publish an allow/deny decision for every read on a Gate IN camera —
              registered and allowlisted vehicles allow, everything else denies.
              Install the <b>Gate Controller</b> app from the catalog to wire the
              decisions to your relay; it ships in dry-run so nothing moves until
              you say so.
              {!Object.values(cameraRoles).some((r) => r.role === 'gate_in') &&
                ' Needs at least one Gate IN camera below.'}
            </div>
          </div>
          <Button
            variant={barrierMode === 'registered' ? 'outline' : 'primary'}
            className={barrierMode === 'registered'
              ? 'text-[var(--danger,#e5484d)] border-[var(--danger,#e5484d)]' : ''}
            onClick={() => onToggleBarrier(barrierMode !== 'registered')}
            disabled={saving}
          >
            {barrierMode === 'registered' ? 'Turn off' : 'Turn on'}
          </Button>
        </CardContent>
      </Card>

      {/* Visitor overstay */}
      <Card>
        <CardContent className="py-3 flex flex-wrap items-center gap-3">
          <History size={18} className={overstayHours > 0 ? 'text-[var(--warning,#b7791f)]' : 'text-[var(--text-dim)]'} />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium">Visitor overstay alert</div>
            <div className="text-xs text-[var(--text-dim)]">
              Alert when a visitor (not registered or allowlisted) has been inside
              longer than this many hours — one alert per visit, checked as reads
              arrive. Needs a Gate IN camera; a Gate OUT camera clears visitors on exit.
            </div>
          </div>
          <label className="text-xs text-[var(--text-dim)]">
            Hours (0 = off)
            <input
              type="number"
              min={0}
              step={0.5}
              value={overstayDraft ?? String(overstayHours || 0)}
              onChange={(e) => setOverstayDraft(e.target.value)}
              className="block mt-0.5 py-1.5 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm text-[var(--text)] w-28"
            />
          </label>
          <Button
            onClick={() => {
              const h = Number(overstayDraft ?? overstayHours)
              if (Number.isFinite(h) && h >= 0) {
                onSetOverstay(h)
                setOverstayDraft(null)
              }
            }}
            disabled={saving || overstayDraft === null}
          >
            Save
          </Button>
        </CardContent>
      </Card>

      {/* Camera roles — the site's layout in the vehicle story */}
      <Card>
        <CardContent className="py-3">
          <div className="text-sm font-medium mb-1">Camera roles</div>
          <div className="text-xs text-[var(--text-dim)] mb-2">
            Give each camera its place: <b>Gate IN</b> (required for gate features),
            <b> Gate OUT</b> (optional — unlocks exits, stay durations and "inside now"),
            <b> Parking</b>, or a named location of your own — every role enriches the
            per-vehicle history.
          </div>
          {(() => {
            const hasIn = Object.values(cameraRoles).some((r) => r.role === 'gate_in')
            const hasOut = Object.values(cameraRoles).some((r) => r.role === 'gate_out')
            if (!hasIn) {
              return (
                <div className="text-xs rounded border border-[var(--warning,#b7791f)] text-[var(--warning,#b7791f)] px-3 py-2 mb-2">
                  No Gate IN camera yet — mark at least one. Until then there is no gate
                  history and no "inside now"; reads still collect normally.
                </div>
              )
            }
            if (!hasOut) {
              return (
                <div className="text-xs rounded border border-[var(--border)] text-[var(--text-dim)] px-3 py-2 mb-2">
                  No Gate OUT camera — entries are recorded, but exit times, stay
                  durations and "inside now" stay off until you mark one.
                </div>
              )
            }
            return null
          })()}
          <div className="flex flex-wrap gap-3">
            {cameras.map((c) => {
              const entry = cameraRoles[String(c.id)]
              return (
                <label key={c.id} className="inline-flex items-center gap-2 text-sm">
                  <span>{c.name}</span>
                  <select
                    value={entry?.role ?? ''}
                    onChange={(e) => {
                      const role = e.target.value as CameraRole | ''
                      onSetRole(c.id, role, role === 'other' ? (entry?.label ?? '') : undefined)
                    }}
                    disabled={saving}
                    className="py-1 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm"
                  >
                    <option value="">no role</option>
                    <option value="gate_in">Gate IN</option>
                    <option value="gate_out">Gate OUT</option>
                    <option value="parking">Parking</option>
                    <option value="other">Other…</option>
                  </select>
                  {entry?.role === 'other' && (
                    <input
                      defaultValue={entry.label ?? ''}
                      placeholder="name it (e.g. Basement)"
                      onBlur={(e) => onSetRole(c.id, 'other', e.target.value.trim())}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                      }}
                      disabled={saving}
                      className="py-1 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm w-36"
                    />
                  )}
                </label>
              )
            })}
            {cameras.length === 0 && (
              <span className="text-xs text-[var(--text-dim)]">No cameras yet.</span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Add + import */}
      <Card>
        <CardContent className="py-3 flex flex-wrap items-end gap-2">
          {([
            ['plate', 'Plate *', 'MH12DE1433', 'text'],
            ['owner', 'Owner', 'A. Sharma', 'text'],
            ['unit', 'Flat / unit', 'B-402', 'text'],
            ['type', 'Type', 'car / truck', 'text'],
            ['model', 'Model', 'Honda City', 'text'],
            ['note', 'Note', '', 'text'],
            ['expires', 'Valid till', '', 'date'],
          ] as const).map(([k, label, ph, kind]) => (
            <label key={k} className="text-xs text-[var(--text-dim)]">
              {label}
              <input
                type={kind}
                value={draft[k] ?? ''}
                onChange={(e) => setDraft((d) => ({ ...d, [k]: e.target.value }))}
                onKeyDown={(e) => { if (e.key === 'Enter') addDraft() }}
                placeholder={ph}
                className="block mt-0.5 py-1.5 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm text-[var(--text)] w-32"
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
              accept=".csv,.xlsx,.xls,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) void importFile(f)
                e.target.value = ''
              }}
            />
            <Button variant="outline" onClick={() => fileRef.current?.click()} disabled={saving}>
              <Upload size={14} /> Import CSV / Excel
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
                  <th className="px-3 py-2">Model</th>
                  <th className="px-3 py-2">Valid till</th>
                  <th className="px-3 py-2">Note</th>
                  <th className="px-3 py-2 text-right pr-4" />
                </tr>
              </thead>
              <tbody>
                {[...registry].sort((a, b) => a.plate.localeCompare(b.plate)).map((r) => (
                  <tr key={r.plate} className="border-b border-[var(--border)] last:border-0 hover:bg-[var(--bg-2)]">
                    <td className="px-3 py-1.5 font-mono font-semibold">
                      {r.plate}
                      {!entryActive(r) && (
                        <Badge variant="warning" className="ml-1.5">expired</Badge>
                      )}
                    </td>
                    <td className="px-3 py-1.5">{r.owner || '—'}</td>
                    <td className="px-3 py-1.5">{r.unit || '—'}</td>
                    <td className="px-3 py-1.5">{r.type || '—'}</td>
                    <td className="px-3 py-1.5">{r.model || '—'}</td>
                    <td className="px-3 py-1.5">{r.expires || '—'}</td>
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

// ── Monitoring (surveillance list) ──────────────────────────────────
// Each monitored plate is ITS OWN rule: why it's watched, how loud the
// alert is, whether it's currently armed, and (optionally) at which
// camera it matters. Edits write through the app's config and apply
// live — the same path as the register and the watchlists.

function MonitoringTab({
  monitors,
  canEdit,
  saving,
  cameras,
  onSave,
}: {
  monitors: Monitor[]
  canEdit: boolean
  saving: boolean
  cameras: CameraRow[]
  onSave: (next: Monitor[]) => void
}) {
  const [draft, setDraft] = useState<{ plate: string; note: string; severity: Monitor['severity'] }>({
    plate: '', note: '', severity: 'high',
  })

  if (!canEdit) {
    return (
      <EmptyState
        icon={<ShieldAlert size={28} />}
        title="Monitoring needs an enabled LPR app"
        description="Install and enable a License Plate Recognition app from the App Catalog — monitors live in that app and apply live."
      />
    )
  }

  const upsert = (m: Monitor) => {
    onSave([...monitors.filter((x) => x.plate !== m.plate), m])
  }

  const addDraft = () => {
    const plate = normalizePlate(draft.plate)
    if (!plate) return
    upsert({
      plate,
      note: draft.note.trim() || undefined,
      severity: draft.severity ?? 'high',
      active: true,
    })
    setDraft({ plate: '', note: '', severity: 'high' })
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="py-3 flex flex-wrap items-end gap-2">
          <label className="text-xs text-[var(--text-dim)]">
            Plate *
            <input
              value={draft.plate}
              onChange={(e) => setDraft((d) => ({ ...d, plate: e.target.value }))}
              onKeyDown={(e) => { if (e.key === 'Enter') addDraft() }}
              placeholder="MH12DE1433"
              className="block mt-0.5 py-1.5 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm text-[var(--text)] w-36"
            />
          </label>
          <label className="text-xs text-[var(--text-dim)]">
            Reason / note
            <input
              value={draft.note}
              onChange={(e) => setDraft((d) => ({ ...d, note: e.target.value }))}
              onKeyDown={(e) => { if (e.key === 'Enter') addDraft() }}
              placeholder="reported stolen — FIR 42/2026"
              className="block mt-0.5 py-1.5 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm text-[var(--text)] w-64"
            />
          </label>
          <label className="text-xs text-[var(--text-dim)]">
            Alert severity
            <select
              value={draft.severity}
              onChange={(e) => setDraft((d) => ({ ...d, severity: e.target.value as Monitor['severity'] }))}
              className="block mt-0.5 py-1.5 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm"
            >
              {MONITOR_SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <Button onClick={addDraft} disabled={saving || !normalizePlate(draft.plate)}>
            <Plus size={14} /> Monitor plate
          </Button>
        </CardContent>
      </Card>

      {monitors.length === 0 ? (
        <EmptyState
          icon={<ShieldAlert size={28} />}
          title="No plates under monitoring"
          description="Add a plate above — or use the shield button on any read — and you'll be alerted the moment it passes a camera, at the severity you choose."
        />
      ) : (
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-[var(--text-dim)] border-b border-[var(--border)]">
                  <th className="px-3 py-2">Plate</th>
                  <th className="px-3 py-2">Reason / note</th>
                  <th className="px-3 py-2">Severity</th>
                  <th className="px-3 py-2">Where</th>
                  <th className="px-3 py-2">Armed</th>
                  <th className="px-3 py-2 text-right pr-4" />
                </tr>
              </thead>
              <tbody>
                {[...monitors].sort((a, b) => a.plate.localeCompare(b.plate)).map((m) => (
                  <tr key={m.plate} className="border-b border-[var(--border)] last:border-0 hover:bg-[var(--bg-2)]">
                    <td className="px-3 py-1.5 font-mono font-semibold">{m.plate}</td>
                    <td className="px-3 py-1.5">
                      <input
                        defaultValue={m.note ?? ''}
                        placeholder="add a reason…"
                        onBlur={(e) => {
                          const v = e.target.value.trim()
                          if (v !== (m.note ?? '')) upsert({ ...m, note: v || undefined })
                        }}
                        disabled={saving}
                        className="w-full bg-transparent border-0 border-b border-transparent focus:border-[var(--border)] outline-none text-sm py-0.5"
                      />
                    </td>
                    <td className="px-3 py-1.5">
                      <select
                        value={m.severity ?? 'high'}
                        onChange={(e) => upsert({ ...m, severity: e.target.value as Monitor['severity'] })}
                        disabled={saving}
                        className="py-1 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm"
                      >
                        {MONITOR_SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
                      </select>
                    </td>
                    <td className="px-3 py-1.5">
                      <select
                        value={(m.cameras ?? [])[0] ?? ''}
                        onChange={(e) => upsert({
                          ...m,
                          cameras: e.target.value ? [e.target.value] : undefined,
                        })}
                        disabled={saving}
                        className="py-1 px-2 rounded border border-[var(--border)] bg-[var(--bg-2)] text-sm"
                      >
                        <option value="">any camera</option>
                        {cameras.map((c) => (
                          <option key={c.id} value={`cam${c.id}`}>{c.name} only</option>
                        ))}
                      </select>
                    </td>
                    <td className="px-3 py-1.5">
                      <button
                        onClick={() => upsert({ ...m, active: m.active === false })}
                        disabled={saving}
                        title={m.active !== false
                          ? 'Armed — click to silence without deleting'
                          : 'Silenced — click to re-arm'}
                      >
                        {m.active !== false
                          ? <Badge variant="destructive">armed</Badge>
                          : <Badge variant="neutral">silenced</Badge>}
                      </button>
                    </td>
                    <td className="px-3 py-1.5 text-right pr-4">
                      <button
                        title="Stop monitoring this plate"
                        className="text-[var(--text-dim)] hover:text-[var(--danger,#e5484d)]"
                        onClick={() => onSave(monitors.filter((x) => x.plate !== m.plate))}
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
      <div className="text-xs text-[var(--text-dim)]">
        Monitored plates never trigger the unknown-vehicle alarm — they fire their own
        alert at the severity set here, and "silenced" keeps the rule without alerting.
      </div>
    </div>
  )
}

// ── The printable monthly report ────────────────────────────────────
// One click → a clean document the society committee or facility
// manager files: totals, registered vehicles grouped by flat/unit,
// visitors, monitored sightings, per-gate counts. "Save as PDF" is the
// browser's print dialog — zero dependencies, works everywhere. The
// print CSS isolates .print-report so only the document prints.

function monthLabel(year: number, month: number): string {
  return new Date(year, month - 1, 1).toLocaleString(undefined, {
    month: 'long', year: 'numeric',
  })
}

function lastMonths(n: number): { year: number; month: number }[] {
  const out: { year: number; month: number }[] = []
  const d = new Date()
  for (let i = 0; i < n; i++) {
    out.push({ year: d.getFullYear(), month: d.getMonth() + 1 })
    d.setMonth(d.getMonth() - 1)
  }
  return out
}

function ReportOverlay({
  registry,
  monitors,
  cameraRoles,
  cameraName,
  onClose,
}: {
  registry: RegistryEntry[]
  monitors: Monitor[]
  cameraRoles: Record<string, CameraRoleEntry>
  cameraName: (id: number) => string
  onClose: () => void
}) {
  const months = useMemo(() => lastMonths(12), [])
  const [sel, setSel] = useState(months[0])

  const reportQuery = useQuery({
    queryKey: ['vehicle-report', sel.year, sel.month],
    queryFn: async () => {
      const { data } = await apiService.getVehicleReport(sel.year, sel.month)
      return data as VehicleReport
    },
    retry: 0,
    staleTime: 60_000,
  })
  const report = reportQuery.data

  const byPlate = useMemo(
    () => new Map(registry.map((r) => [r.plate, r] as const)),
    [registry]
  )
  const monitorByPlate = useMemo(
    () => new Map(monitors.map((m) => [m.plate, m] as const)),
    [monitors]
  )

  // Registered rows grouped by unit; everything else is a visitor.
  const { registered, visitors, monitored } = useMemo(() => {
    const reg: { unit: string; entry: RegistryEntry; reads: number; last: string | null }[] = []
    const vis: VehicleReport['per_plate'] = []
    const mon: { plate: string; note?: string; reads: number; last: string | null }[] = []
    for (const p of report?.per_plate ?? []) {
      const m = monitorByPlate.get(p.plate)
      if (m) mon.push({ plate: p.plate, note: m.note, reads: p.reads, last: p.last_seen })
      const entry = byPlate.get(p.plate)
      if (entry) {
        reg.push({ unit: entry.unit || '—', entry, reads: p.reads, last: p.last_seen })
      } else {
        vis.push(p)
      }
    }
    reg.sort((a, b) => a.unit.localeCompare(b.unit) || a.entry.plate.localeCompare(b.entry.plate))
    return { registered: reg, visitors: vis.slice(0, 60), monitored: mon }
  }, [report, byPlate, monitorByPlate])

  const busiestDay = useMemo(() => {
    const days = report?.per_day ?? []
    if (!days.length) return null
    return [...days].sort((a, b) => b.reads - a.reads)[0]
  }, [report])

  const dt = (v: string | null | undefined) =>
    v ? new Date(v).toLocaleString(undefined, {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
    }) : '—'

  return (
    <div className="fixed inset-0 z-50 bg-black/50 overflow-y-auto print:overflow-visible">
      <style>{`
        @media print {
          body * { visibility: hidden !important; }
          .print-report, .print-report * { visibility: visible !important; }
          .print-report { position: absolute !important; inset: 0 !important;
            margin: 0 !important; box-shadow: none !important;
            border-radius: 0 !important; }
          .no-print { display: none !important; }
        }
      `}</style>
      <div className="print-report bg-white text-neutral-900 max-w-3xl mx-auto my-6 rounded-lg shadow-xl p-8 print:p-0">
        {/* controls (screen only) */}
        <div className="no-print flex items-center gap-2 mb-6 pb-4 border-b border-neutral-200">
          <select
            value={`${sel.year}-${sel.month}`}
            onChange={(e) => {
              const [y, m] = e.target.value.split('-').map(Number)
              setSel({ year: y, month: m })
            }}
            className="py-1.5 px-2 rounded border border-neutral-300 bg-white text-sm"
          >
            {months.map((m) => (
              <option key={`${m.year}-${m.month}`} value={`${m.year}-${m.month}`}>
                {monthLabel(m.year, m.month)}
              </option>
            ))}
          </select>
          <Button onClick={() => window.print()} disabled={!report}>
            <FileText size={14} /> Print / Save as PDF
          </Button>
          <button
            className="ml-auto text-neutral-500 hover:text-neutral-900 text-sm"
            onClick={onClose}
          >
            ✕ Close
          </button>
        </div>

        {reportQuery.isPending ? (
          <Skeleton className="h-64" />
        ) : reportQuery.isError ? (
          <div className="text-sm text-neutral-500">
            {extractApiError(reportQuery.error, 'Could not build the report.')}
          </div>
        ) : report && (
          <div className="text-sm leading-relaxed">
            {/* header */}
            <div className="mb-6">
              <div className="text-2xl font-semibold">Vehicle Movement Report</div>
              <div className="text-neutral-500">
                {monthLabel(report.year, report.month)} · generated {new Date().toLocaleDateString()} · OpenNVR
              </div>
            </div>

            {/* summary */}
            <div className="grid grid-cols-4 gap-3 mb-6">
              {[
                ['Total reads', report.total_reads],
                ['Unique vehicles', report.unique_plates],
                ['Registered seen', registered.length],
                ['Visitors / unknown', visitors.length],
              ].map(([label, value]) => (
                <div key={String(label)} className="border border-neutral-200 rounded p-3">
                  <div className="text-xl font-semibold">{value}</div>
                  <div className="text-xs text-neutral-500">{label}</div>
                </div>
              ))}
            </div>
            <div className="mb-6 text-neutral-600">
              {(report.per_camera ?? []).map((c) => {
                const label = roleLabel(cameraRoles[String(c.camera_id)])
                return `${cameraName(c.camera_id)}${label ? ` (${label})` : ''}: ${c.reads} reads`
              }).join(' · ')}
              {busiestDay && ` · busiest day ${busiestDay.day} (${busiestDay.reads})`}
            </div>

            {/* registered by unit */}
            <div className="text-base font-semibold mb-1 mt-6">Registered vehicles</div>
            {registered.length === 0 ? (
              <div className="text-neutral-500">No registered vehicle was seen this month.</div>
            ) : (
              <table className="w-full border-collapse mb-2">
                <thead>
                  <tr className="text-left text-xs text-neutral-500 border-b border-neutral-300">
                    <th className="py-1 pr-2">Flat / unit</th>
                    <th className="py-1 pr-2">Plate</th>
                    <th className="py-1 pr-2">Owner</th>
                    <th className="py-1 pr-2">Model</th>
                    <th className="py-1 pr-2 text-right">Visits</th>
                    <th className="py-1 pl-3">Last seen</th>
                  </tr>
                </thead>
                <tbody>
                  {registered.map((r) => (
                    <tr key={r.entry.plate} className="border-b border-neutral-100">
                      <td className="py-1 pr-2">{r.unit}</td>
                      <td className="py-1 pr-2 font-mono font-semibold">{r.entry.plate}</td>
                      <td className="py-1 pr-2">{r.entry.owner || '—'}</td>
                      <td className="py-1 pr-2">{r.entry.model || r.entry.type || '—'}</td>
                      <td className="py-1 pr-2 text-right font-mono">{r.reads}</td>
                      <td className="py-1 pl-3 text-neutral-600">{dt(r.last)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {/* visitors */}
            <div className="text-base font-semibold mb-1 mt-6">Visitors &amp; unknown vehicles</div>
            {visitors.length === 0 ? (
              <div className="text-neutral-500">No unregistered vehicle was seen this month.</div>
            ) : (
              <table className="w-full border-collapse mb-2">
                <thead>
                  <tr className="text-left text-xs text-neutral-500 border-b border-neutral-300">
                    <th className="py-1 pr-2">Plate</th>
                    <th className="py-1 pr-2 text-right">Visits</th>
                    <th className="py-1 pl-3">First seen</th>
                    <th className="py-1 pl-3">Last seen</th>
                  </tr>
                </thead>
                <tbody>
                  {visitors.map((v) => (
                    <tr key={v.plate} className="border-b border-neutral-100">
                      <td className="py-1 pr-2 font-mono font-semibold">{v.plate}</td>
                      <td className="py-1 pr-2 text-right font-mono">{v.reads}</td>
                      <td className="py-1 pl-3 text-neutral-600">{dt(v.first_seen)}</td>
                      <td className="py-1 pl-3 text-neutral-600">{dt(v.last_seen)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {/* monitored */}
            {monitored.length > 0 && (
              <>
                <div className="text-base font-semibold mb-1 mt-6">Monitored plate sightings</div>
                <table className="w-full border-collapse mb-2">
                  <thead>
                    <tr className="text-left text-xs text-neutral-500 border-b border-neutral-300">
                      <th className="py-1 pr-2">Plate</th>
                      <th className="py-1 pr-2">Reason</th>
                      <th className="py-1 pr-2 text-right">Sightings</th>
                      <th className="py-1 pl-3">Last seen</th>
                    </tr>
                  </thead>
                  <tbody>
                    {monitored.map((m) => (
                      <tr key={m.plate} className="border-b border-neutral-100">
                        <td className="py-1 pr-2 font-mono font-semibold">{m.plate}</td>
                        <td className="py-1 pr-2">{m.note || '—'}</td>
                        <td className="py-1 pr-2 text-right font-mono">{m.reads}</td>
                        <td className="py-1 pl-3 text-neutral-600">{dt(m.last)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

            <div className="mt-8 pt-3 border-t border-neutral-200 text-xs text-neutral-400">
              Generated by OpenNVR · plate reads are recorded with photographic
              evidence; individual entries can be verified on the Vehicles page.
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
