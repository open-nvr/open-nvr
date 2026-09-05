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

// App Catalog: detector apps built on the OpenNVR App SDK. Cards read from
// GET /api/v1/apps; each checks its manifest.requires_tasks against the tasks
// advertised by KAI-C adapters, so the operator can see whether the backing
// model is present before enabling. Config forms are generated from the
// manifest param schema — no app-specific UI code.

import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, ArrowRight, BadgeCheck, Boxes, Check, Copy, Download, ExternalLink, KeyRound, RefreshCw, Settings2, Trash2 } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { useAuth } from '../auth/AuthContext'
import { extractApiError } from '../lib/apiError'
import { Modal } from '../components/Modal'
import { useSnackbar } from '../components/Snackbar'
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, EmptyState, ErrorCard, PageHeader, Skeleton, type BadgeVariant } from '../components/ui'
import { GeometryEditor } from './apps/GeometryEditor'
import { ChipListEditor } from './apps/ChipListEditor'
import { TimeWindowEditor } from './apps/TimeWindowEditor'

export type ManifestParam = {
  name: string
  type: string
  default?: any
  per_camera?: boolean
  required?: boolean
  description?: string
}

export type AppManifest = {
  id: string
  name: string
  version: string
  category: string
  summary?: string
  requires_tasks?: string[]
  subscribes?: string[]
  params?: ManifestParam[]
  emits?: { name: string; severity?: string; description?: string }[]
  // Declarative live-state views (SDK StateView.to_dict()) — rendered
  // generically by LiveStateViews below; absent for older manifests.
  state_schema?: {
    name: string
    label: string
    kind: 'metric' | 'table'
    path?: string
    columns?: string[]
    description?: string
  }[]
  // Declarative operator actions (SDK Action.to_dict()) — generic forms
  // POSTed through the server's user-JWT-only proxy.
  actions?: ManifestAction[]
  // RFC-0002 Phase 4: the app serves an HTML dashboard at /ui on its
  // contract port; AppView renders it via core's proxy, sandboxed.
  has_ui?: boolean
  // RFC-0002 Phase 5: event scopes the app requested (granted at
  // registration, each grant audited) — plate reads are PII, so who
  // consumes them is visible here, not buried in code.
  requires_scopes?: string[]
  // How the app's UI reaches the operator: "internal" (default) embeds
  // the sandboxed /ui dashboard; "external" means the app is a full
  // application — the catalog shows an "Open app" button to ui_url
  // ("{host}" in it is replaced with the browser's hostname).
  ui_mode?: 'internal' | 'external'
  ui_url?: string
  // Store listing (the Details section): long-form description
  // (blank-line-separated paragraphs), authorship, and the concrete
  // jobs the app solves.
  description?: string
  author?: string
  website?: string
  license?: string
  use_cases?: string[]
  // Where a deployment can reach the developer for custom work
  // (mailto: or https:) — rendered as a "Contact" action.
  contact?: string
  // Vertical capabilities this app provides ("vehicles", "occupancy")
  // — first-class pages in the main nav light up per capability.
  provides?: string[]
  // Commerce: free | paid | subscription | contact, a human price line,
  // and whether enabling needs a licence key the app verifies.
  pricing?: 'free' | 'paid' | 'subscription' | 'contact'
  price_note?: string
  entitlement?: 'none' | 'license_key'
}

// The platform's record of a licensed app's key (never the key itself)
// and the app's own verdict on it — GET /apps and the config poll.
export type Entitlement = {
  mode: 'none' | 'license_key'
  status: 'none' | 'unverified' | 'valid' | 'invalid'
  plan?: string | null
  expires_at?: string | null
  message?: string
  limits?: Record<string, any>
  checked_at?: string | null
  has_license_key: boolean
}

/** A small pricing badge for cards and listings; nothing for free apps. */
function PricingBadge({ pricing, note }: { pricing?: string; note?: string }) {
  if (!pricing || pricing === 'free') return null
  const label = pricing === 'contact' ? 'contact for pricing' : pricing
  return (
    <Badge variant="neutral" title={note || undefined}>
      {label}{note ? ` · ${note}` : ''}
    </Badge>
  )
}

/** "Licence key required" — shown BEFORE install so an operator knows a
 * licensed app will not enable until an administrator enters a key the
 * app accepts (the installed card then carries the LicensePanel). */
function LicenceRequiredBadge({ entitlement }: { entitlement?: string }) {
  if (entitlement !== 'license_key') return null
  return (
    <Badge
      variant="warning"
      title="Installs normally, but cannot be enabled until an administrator enters a licence key the app verifies"
    >
      <KeyRound size={12} /> licence key required
    </Badge>
  )
}

/** The maintainers vouch for this author — identity confirmed, image
 * reproducible from the linked source. Set by reviewers in the index. */
function VerifiedBadge({ verified, author }: { verified?: boolean; author?: string }) {
  if (!verified) return null
  return (
    <Badge
      variant="success"
      title={`Verified developer${author ? `: ${author}` : ''} — identity confirmed and image reproducible from source, per OpenNVR's app review`}
    >
      <BadgeCheck size={12} /> verified
    </Badge>
  )
}

/** Resolve an external ui_url for THIS browser: apps rarely know their
 * deployment hostname at build time, so "{host}" is substituted with
 * wherever the operator is browsing from. */
export function resolveUiUrl(uiUrl: string): string {
  return uiUrl.split('{host}').join(window.location.hostname)
}

export type ManifestAction = {
  name: string
  label: string
  params?: ManifestParam[]
  description?: string
  confirm?: boolean
}

export type RegisteredApp = {
  id: string
  name: string
  category: string
  version: string
  url: string
  enabled: boolean
  status?: string
  last_seen?: string | null
  manifest?: AppManifest | null
  config?: Record<string, any> | null
  entitlement?: Entitlement | null
}

export type AppStatusResp = {
  health?: { status?: string; [k: string]: any } | null
  state?: any
}

// An entry from GET /api/v1/apps/index — the store listing. Entries with
// installed=true are already registered and surface under "Installed" instead.
type IndexApp = {
  id: string
  name: string
  summary?: string
  category: string
  version: string
  // "installable" (image + compose service) or "external" (a listing
  // that links out — no Install button, a "Learn more" link instead).
  kind?: 'installable' | 'external'
  external_url?: string | null
  pricing?: 'free' | 'paid' | 'subscription' | 'contact'
  price_note?: string
  entitlement?: 'none' | 'license_key'
  author?: string
  // Reviewer-set curation flags (never from the submitter's manifest).
  verified?: boolean
  featured?: boolean
  image?: string | null
  requires_tasks?: string[]
  emits?: string[]
  docs_url?: string
  install?: { compose?: string; command?: string } | null
  installed: boolean
  enabled: boolean | null
}

type CapabilitiesResp = { kai_c?: Record<string, any>; adapters?: Record<string, Record<string, any>> }

// One entry from GET /api/v1/skills (RFC-0002 Phase 1) — the platform
// registry's view of a capability: who provides it, its lifecycle status
// derived from real signals, and the contracted events it publishes.
export type SkillEntry = {
  id: string
  name: string
  provider: { kind: 'app' | 'adapter'; providers: string[] }
  status: 'available' | 'dormant' | 'active' | 'degraded' | 'missing-dependency'
  reason?: string | null
  publishes: string[]
}

// Reconciler-driven install lifecycle. install/uninstall POST an intent that
// lands "pending"; the reconciler later flips it to "applied" or "failed".
type InstallStatusPhase = 'none' | 'pending' | 'applied' | 'failed'
type InstallStatusResp = {
  id: string // the app id (the server serializes `id`, not `app_id`)
  status: InstallStatusPhase
  message?: string
  image?: string
  image_digest?: string
}

// Stop polling after ~5 minutes of "pending": the reconciler is a
// separately-deployed OPT-IN component, so an intent can legitimately sit
// pending forever (installer container not running) — without a cap the UI
// polls every 2s per app per tab indefinitely with the buttons stuck on
// "Installing…". After the cap we stop and surface a hint instead.
const INSTALL_POLL_MAX_MS = 5 * 60 * 1000

// The install/uninstall endpoints are opt-in + RBAC gated: a 403 is a normal,
// expected path (operator disabled one-click, or caller lacks apps.install) —
// NOT an error to toast. Callers treat it as "fall back to the command."
function isForbidden(e: any): boolean {
  return e?.status === 403 || e?.response?.status === 403
}

function useApps() {
  return useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
  })
}

// The index is best-effort: if the endpoint 404s or errors, the Installed group
// still renders. retry:0 keeps a missing endpoint from thrashing.
function useAppIndex() {
  return useQuery({
    queryKey: ['apps-index'],
    queryFn: async () => {
      const { data } = await apiService.getAppIndex()
      const apps = (data as { apps?: unknown })?.apps
      return (Array.isArray(apps) ? apps : []) as IndexApp[]
    },
    retry: 0,
  })
}

// Best-effort like the index: an older backend without /skills (or an
// errored fetch) simply omits the per-app skill line. retry:0.
function useSkillsRegistry() {
  return useQuery({
    queryKey: ['skills-registry'],
    queryFn: async () => {
      const { data } = await apiService.getSkills()
      const skills = (data as { skills?: unknown })?.skills
      return (Array.isArray(skills) ? skills : []) as SkillEntry[]
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

// Polls install-status while a reconcile is in flight. Enabled only after an
// install/uninstall intent is accepted (`active`); refetches on an interval
// while pending and stops once the reconciler reports applied/failed — or
// once the poll budget is spent (the installer may simply not be running).
// On applied it invalidates ['apps'] + ['apps-index'] so the app hops groups.
function useInstallStatusPoll(id: string, active: boolean) {
  const queryClient = useQueryClient()
  const startedAtRef = useRef<number | null>(null)
  const [timedOut, setTimedOut] = useState(false)
  useEffect(() => {
    // Reset the budget each time a new intent starts polling.
    startedAtRef.current = active ? Date.now() : null
    setTimedOut(false)
  }, [active, id])
  const query = useQuery({
    queryKey: ['app-install-status', id],
    queryFn: async () => {
      const { data } = await apiService.getInstallStatus(id)
      return data as InstallStatusResp
    },
    enabled: active && !timedOut,
    retry: 0,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (status === 'applied') {
        queryClient.invalidateQueries({ queryKey: ['apps'] })
        queryClient.invalidateQueries({ queryKey: ['apps-index'] })
        return false
      }
      if (status !== 'pending') return false
      const started = startedAtRef.current
      if (started !== null && Date.now() - started > INSTALL_POLL_MAX_MS) {
        setTimedOut(true)
        return false
      }
      return 2000
    },
  })
  return { ...query, timedOut }
}

function InstallStatusNote({ status }: { status: InstallStatusResp }) {
  const variant: BadgeVariant =
    status.status === 'applied' ? 'success' : status.status === 'failed' ? 'destructive' : 'warning'
  const label =
    status.status === 'applied'
      ? 'applied'
      : status.status === 'failed'
        ? 'failed'
        : status.status === 'pending'
          ? 'pending'
          : status.status
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <Badge variant={variant}>{label}</Badge>
      {status.message && <span className="text-[var(--text-dim)]">{status.message}</span>}
      {status.image && <code className="text-xs text-[var(--text-dim)]">{status.image}</code>}
    </div>
  )
}

export function asStringList(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(String)
  return []
}

/** Union of every task advertised by any adapter registered with KAI-C. */
function availableTasks(caps: CapabilitiesResp | undefined): Set<string> {
  const tasks = new Set<string>()
  for (const info of Object.values(caps?.adapters ?? {})) {
    for (const t of asStringList(info?.tasks_advertised).concat(asStringList(info?.tasks))) tasks.add(t)
  }
  return tasks
}

export function statusVariant(status?: string): BadgeVariant {
  switch ((status || '').toLowerCase()) {
    case 'ok':
    case 'ready':
    case 'running':
    case 'healthy':
    case 'online':
      return 'success'
    case 'degraded':
    case 'starting':
    case 'loading':
      return 'warning'
    case 'error':
    case 'unhealthy':
    case 'unreachable':
    case 'offline':
      return 'destructive'
    default:
      return 'neutral'
  }
}

/** Params whose values aren't scalar edit as JSON in the generated form. */
function isJsonParam(p: ManifestParam): boolean {
  const t = (p.type || '').toLowerCase()
  return p.per_camera === true || t === 'list' || t.startsWith('geometry.') || t === 'dict' || t === 'json' || t === 'time_range'
}

function initialFormValue(p: ManifestParam, config: Record<string, any> | null | undefined): string | boolean {
  const current = config && p.name in config ? config[p.name] : p.default
  if (p.type === 'bool' && !isJsonParam(p)) return Boolean(current)
  if (isJsonParam(p)) return current === undefined ? '' : JSON.stringify(current, null, 2)
  return current === undefined || current === null ? '' : String(current)
}

/* ------------------------- Config modal ------------------------- */

export function AppConfigModal({ app, onClose }: { app: RegisteredApp; onClose: () => void }) {
  const queryClient = useQueryClient()
  const { showSuccess } = useSnackbar()
  const params = app.manifest?.params ?? []
  // Site-wide params are superuser-only on the server; anyone else may
  // draw/edit only the per-camera entries of cameras they manage (the
  // server merges those into the stored config and refuses the rest).
  const { user: me } = useAuth()
  const isAdmin = !!me?.is_superuser
  const canEditParam = (p: ManifestParam) => isAdmin || p.per_camera === true
  const [values, setValues] = useState<Record<string, string | boolean>>(() =>
    Object.fromEntries(params.map((p) => [p.name, initialFormValue(p, app.config)]))
  )
  const [error, setError] = useState<string | null>(null)

  const saveMutation = useMutation({
    mutationFn: (config: Record<string, any>) => apiService.updateAppConfig(app.id, config),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      showSuccess(`${app.name} configuration saved`)
      onClose()
    },
    onError: (e) => setError(extractApiError(e, 'Failed to save app configuration.')),
  })

  const submit = () => {
    setError(null)
    const config: Record<string, any> = {}
    for (const p of params) {
      if (!canEditParam(p)) continue   // untouched site-wide keys stay as stored
      const raw = values[p.name]
      const t = (p.type || '').toLowerCase()
      if (isJsonParam(p)) {
        const text = String(raw ?? '').trim()
        if (!text) {
          if (p.required) {
            setError(`"${p.name}" is required.`)
            return
          }
          continue
        }
        try {
          config[p.name] = JSON.parse(text)
        } catch {
          setError(`"${p.name}" is not valid JSON.`)
          return
        }
      } else if (t === 'bool') {
        config[p.name] = Boolean(raw)
      } else if (t === 'int' || t === 'float') {
        const text = String(raw ?? '').trim()
        if (!text) {
          if (p.required) {
            setError(`"${p.name}" is required.`)
            return
          }
          continue
        }
        const num = Number(text)
        if (!Number.isFinite(num) || (t === 'int' && !Number.isInteger(num))) {
          setError(`"${p.name}" must be a valid ${t === 'int' ? 'integer' : 'number'}.`)
          return
        }
        config[p.name] = num
      } else {
        const text = String(raw ?? '')
        if (!text && p.required) {
          setError(`"${p.name}" is required.`)
          return
        }
        if (text || !p.required) config[p.name] = text
      }
    }
    saveMutation.mutate(config)
  }

  return (
    <Modal
      open
      title={`Configure ${app.name}`}
      onClose={onClose}
      widthClassName={
        params.some((p) => (p.type || '').toLowerCase().startsWith('geometry.'))
          ? 'w-[720px]'
          : 'w-[560px]'
      }
    >
      {params.length === 0 ? (
        <div className="text-sm text-[var(--text-dim)]">This app declares no configurable parameters.</div>
      ) : (
        <div className="space-y-4">
          {params.map((p) => {
            const t = (p.type || '').toLowerCase()
            const value = values[p.name]
            return (
              <div key={p.name}>
                <label className="block text-sm mb-1">
                  <span className="font-medium">{p.name}</span>
                  <span className="ml-2 text-xs text-[var(--text-dim)]">
                    {p.type}
                    {p.per_camera ? ' · per camera' : ''}
                    {p.required ? ' · required' : ''}
                  </span>
                </label>
                {p.description && <div className="text-xs text-[var(--text-dim)] mb-1">{p.description}</div>}
                {!canEditParam(p) ? (
                  <div
                    className="w-full px-2 py-1.5 text-sm font-mono rounded border border-[var(--border)] bg-[var(--bg-2)] text-[var(--text-dim)] whitespace-pre-wrap break-all"
                    title="Site-wide setting — only an administrator can change it"
                  >
                    {String(value ?? '') || '—'}
                    <span className="ml-2 text-[11px] font-sans">(administrator only)</span>
                  </div>
                ) : t === 'geometry.polygon' || t === 'geometry.tripwire' ? (
                  <GeometryEditor
                    kind={t === 'geometry.tripwire' ? 'tripwire' : 'polygon'}
                    value={String(value ?? '')}
                    onChange={(json) => setValues((v) => ({ ...v, [p.name]: json }))}
                  />
                ) : t === 'list' && !p.per_camera ? (
                  <ChipListEditor
                    value={String(value ?? '')}
                    placeholder={`add ${p.name} value, Enter`}
                    onChange={(json) => setValues((v) => ({ ...v, [p.name]: json }))}
                  />
                ) : t === 'time_range' ? (
                  <TimeWindowEditor
                    value={String(value ?? '')}
                    onChange={(range) => setValues((v) => ({ ...v, [p.name]: range }))}
                  />
                ) : isJsonParam(p) ? (
                  <textarea
                    className="w-full h-28 px-2 py-1.5 text-sm font-mono rounded border border-[var(--border)] bg-[var(--bg-2)] text-[var(--text)]"
                    value={String(value ?? '')}
                    placeholder={p.per_camera ? '{"camera_id": …}' : '[…]'}
                    onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.value }))}
                  />
                ) : t === 'bool' ? (
                  <label className="inline-flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={Boolean(value)}
                      onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.checked }))}
                    />
                    Enabled
                  </label>
                ) : (
                  <input
                    type={t === 'int' || t === 'float' ? 'number' : 'text'}
                    step={t === 'float' ? 'any' : undefined}
                    className="w-full px-2 py-1.5 text-sm rounded border border-[var(--border)] bg-[var(--bg-2)] text-[var(--text)]"
                    value={String(value ?? '')}
                    onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.value }))}
                  />
                )}
              </div>
            )
          })}
        </div>
      )}

      {error && <div className="mt-3 text-sm text-red-400">{error}</div>}

      <div className="mt-4 flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        {params.length > 0 && (
          <Button variant="primary" onClick={submit} disabled={saveMutation.isPending}>
            {saveMutation.isPending ? 'Saving…' : 'Save'}
          </Button>
        )}
      </div>
    </Modal>
  )
}

/* ------------------- Image param (action forms) ------------------ */

// A file picker for `image`/`file` action params. Reads the chosen
// image as RAW base64 (data: prefix stripped) into the param value and
// shows a thumbnail. Used by smart-doorbell's face enrollment.
function ImageParamInput({ hasValue, onPick }: { hasValue: boolean; onPick: (b64: string) => void }) {
  const [preview, setPreview] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const MAX_BYTES = 6 * 1024 * 1024 // under the SDK's 8MB action-body cap after base64

  const onFile = (file: File | undefined) => {
    setErr(null)
    if (!file) return
    if (!file.type.startsWith('image/')) return setErr('Please choose an image file.')
    if (file.size > MAX_BYTES) return setErr('Image is too large (max ~6 MB).')
    const reader = new FileReader()
    reader.onload = () => {
      const dataUrl = String(reader.result || '')
      setPreview(dataUrl)
      onPick(dataUrl.includes(',') ? dataUrl.split(',', 2)[1] : dataUrl) // raw base64
    }
    reader.onerror = () => setErr('Could not read the file.')
    reader.readAsDataURL(file)
  }

  return (
    <div className="space-y-1.5">
      <input
        type="file"
        accept="image/*"
        className="block w-full text-sm text-[var(--text-dim)] file:mr-2 file:px-2 file:py-1 file:rounded file:border file:border-[var(--border)] file:bg-[var(--bg-2)] file:text-[var(--text)]"
        onChange={(e) => onFile(e.target.files?.[0])}
      />
      {preview && (
        <img src={preview} alt="selected" className="h-24 rounded border border-[var(--border)] object-cover" />
      )}
      {!preview && hasValue && <div className="text-xs text-[var(--text-dim)]">image selected</div>}
      {err && <div className="text-xs text-red-400">{err}</div>}
    </div>
  )
}

/* --------------------------- Action modal ------------------------ */

// Generic form for one manifest-declared action — the same
// params-to-form mechanics as the config modal, POSTed through the
// server's user-JWT-only proxy to the app's /actions/{name}. Results
// with a list under "results" reuse the TableView renderer; anything
// else pretty-prints.
export function AppActionModal({
  app,
  action,
  onClose,
}: {
  app: RegisteredApp
  action: ManifestAction
  onClose: () => void
}) {
  const params = action.params ?? []
  const [values, setValues] = useState<Record<string, string | boolean>>(() =>
    Object.fromEntries(
      params.map((p) => [
        p.name,
        (p.type || '').toLowerCase() === 'bool'
          ? Boolean(p.default)
          : p.default == null
            ? ''
            : typeof p.default === 'object'
              ? JSON.stringify(p.default)
              : String(p.default),
      ])
    )
  )
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<any>(null)

  const runMutation = useMutation({
    mutationFn: (payload: Record<string, any>) =>
      apiService.invokeAppAction(app.id, action.name, payload),
    onSuccess: ({ data }) => {
      setError(null)
      setResult(data)
    },
    onError: (e) => setError(extractApiError(e, `${action.label} failed.`)),
  })

  const submit = () => {
    setError(null)
    const payload: Record<string, any> = {}
    for (const p of params) {
      const raw = values[p.name]
      const t = (p.type || '').toLowerCase()
      const text = String(raw ?? '').trim()
      if (t === 'bool') {
        payload[p.name] = Boolean(raw)
      } else if (t === 'int' || t === 'float') {
        if (!text) {
          if (p.required) return setError(`"${p.name}" is required.`)
          continue
        }
        const num = Number(text)
        if (!Number.isFinite(num) || (t === 'int' && !Number.isInteger(num))) {
          return setError(`"${p.name}" must be a valid ${t === 'int' ? 'integer' : 'number'}.`)
        }
        payload[p.name] = num
      } else if (t === 'list' || t === 'dict') {
        if (!text) {
          if (p.required) return setError(`"${p.name}" is required.`)
          continue
        }
        try {
          payload[p.name] = JSON.parse(text)
        } catch {
          return setError(`"${p.name}" is not valid JSON.`)
        }
      } else {
        if (!text && p.required) return setError(`"${p.name}" is required.`)
        if (text || !p.required) payload[p.name] = text
      }
    }
    if (action.confirm && !window.confirm(`Run "${action.label}" on ${app.name}?`)) return
    runMutation.mutate(payload)
  }

  const resultsList = Array.isArray(result?.results) ? result.results : null

  return (
    <Modal open title={`${action.label} — ${app.name}`} onClose={onClose} widthClassName="w-[640px]">
      <div className="space-y-3">
        {action.description && (
          <div className="text-sm text-[var(--text-dim)]">{action.description}</div>
        )}
        {params.map((p) => {
          const t = (p.type || '').toLowerCase()
          const value = values[p.name]
          return (
            <div key={p.name}>
              <label className="block text-sm mb-1">
                <span className="font-medium">{p.name}</span>
                <span className="ml-2 text-xs text-[var(--text-dim)]">
                  {p.type}
                  {p.required ? ' · required' : ''}
                </span>
              </label>
              {p.description && (
                <div className="text-xs text-[var(--text-dim)] mb-1">{p.description}</div>
              )}
              {t === 'bool' ? (
                <label className="inline-flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={Boolean(value)}
                    onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.checked }))}
                  />
                  Enabled
                </label>
              ) : t === 'image' || t === 'file' ? (
                <ImageParamInput
                  hasValue={Boolean(String(value ?? ''))}
                  onPick={(b64) => setValues((v) => ({ ...v, [p.name]: b64 }))}
                />
              ) : (
                <input
                  type={t === 'int' || t === 'float' ? 'number' : 'text'}
                  step={t === 'float' ? 'any' : undefined}
                  className="w-full px-2 py-1.5 text-sm rounded border border-[var(--border)] bg-[var(--bg-2)] text-[var(--text)]"
                  value={String(value ?? '')}
                  onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.value }))}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') submit()
                  }}
                />
              )}
            </div>
          )
        })}

        {error && <div className="text-sm text-red-400">{error}</div>}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Close</Button>
          <Button variant="primary" onClick={submit} disabled={runMutation.isPending}>
            {runMutation.isPending ? 'Running…' : action.label}
          </Button>
        </div>

        {result != null && (
          <div className="pt-2 border-t border-[var(--border)] space-y-2">
            {resultsList ? (
              resultsList.length === 0 ? (
                <div className="text-sm text-[var(--text-dim)]">No results.</div>
              ) : (
                <TableView
                  view={{ name: 'results', label: `Results (${resultsList.length})`, kind: 'table' }}
                  value={resultsList}
                />
              )
            ) : (
              <pre className="text-xs bg-[var(--bg-2)] border border-[var(--border)] rounded p-2 overflow-x-auto">
                {JSON.stringify(result, null, 2)}
              </pre>
            )}
          </div>
        )}
      </div>
    </Modal>
  )
}

/* -------------------------- Status chip -------------------------- */

// Lazy: only fetched after the operator clicks "Check" — the registry list is
// cheap, but /status proxies to each app's /health, so don't fan out on mount.
function AppStatusChip({ appId }: { appId: string }) {
  const [requested, setRequested] = useState(false)
  const statusQuery = useQuery({
    queryKey: ['app-status', appId],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(appId)
      return data as AppStatusResp
    },
    enabled: requested,
    retry: 0,
  })

  if (!requested) {
    return (
      <Button variant="ghost" className="text-xs px-2 py-1" onClick={() => setRequested(true)}>
        <Activity size={12} /> Check
      </Button>
    )
  }
  if (statusQuery.isPending) return <Skeleton className="h-5 w-16" />
  if (statusQuery.isError) {
    return <Badge variant="destructive">{extractApiError(statusQuery.error, 'status check failed')}</Badge>
  }
  const health = statusQuery.data?.health?.status ?? 'unknown'
  return (
    <span className="inline-flex items-center gap-1">
      <Badge variant={statusVariant(health)}>{health}</Badge>
      <Button variant="ghost" className="text-xs px-1.5 py-1" onClick={() => statusQuery.refetch()} title="Re-check status">
        <RefreshCw size={12} className={statusQuery.isFetching ? 'animate-spin' : ''} />
      </Button>
    </span>
  )
}

/* ----------------------- Declarative state views ------------------ */

// Manifest-declared views over GET /state (SDK StateView) — the same
// zero-app-specific-UI bet as params → config form. Rendered from the
// SAME react-query cache entry the status chip fills, so the live state
// appears when the operator clicks "Check" and refreshes with it.
export type StateViewSpec = {
  name: string
  label: string
  kind: 'metric' | 'table' | 'gauge' | 'log' | 'gallery'
  path?: string
  columns?: string[]
  description?: string
  // gauge
  min?: number
  max?: number
  unit?: string
  warn?: number
  danger?: number
  // log / gallery
  limit?: number
}

function getStatePath(obj: any, path?: string): any {
  if (!path) return obj
  return path.split('.').reduce((acc: any, key: string) => (acc == null ? undefined : acc[key]), obj)
}

function MetricView({ view, value }: { view: StateViewSpec; value: any }) {
  let display: string
  if (value == null) display = '—'
  else if (Array.isArray(value)) display = String(value.length)
  else if (typeof value === 'object') display = String(Object.keys(value).length)
  else display = String(value)
  return (
    <div
      className="rounded border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-xs"
      title={view.description || view.name}
    >
      <span className="text-[var(--text-dim)]">{view.label}</span>{' '}
      <span className="font-semibold">{display}</span>
    </div>
  )
}

function TableView({ view, value }: { view: StateViewSpec; value: any }) {
  // Accept a list of dicts, a list of scalars, or a dict-of-dicts
  // (rendered with the key as a leading "id" column) — /state shapes
  // vary and the renderer must never crash on live data.
  let rows: Record<string, any>[] = []
  if (Array.isArray(value)) {
    rows = value.map((v) => (v != null && typeof v === 'object' ? v : { value: v }))
  } else if (value != null && typeof value === 'object') {
    rows = Object.entries(value).map(([k, v]) => ({
      id: k,
      ...(v != null && typeof v === 'object' ? (v as Record<string, any>) : { value: v }),
    }))
  }
  if (rows.length === 0) return null
  const columns =
    view.columns && view.columns.length > 0
      ? view.columns.filter((c) => rows.some((r) => c in r))
      : Array.from(new Set(rows.flatMap((r) => Object.keys(r)))).slice(0, 6)
  if (columns.length === 0) return null
  return (
    <div className="overflow-x-auto" title={view.description || view.name}>
      <div className="text-xs text-[var(--text-dim)] mb-1">{view.label}</div>
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c} className="text-left px-2 py-1 border-b border-[var(--border)] text-[var(--text-dim)] font-normal">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 25).map((row, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c} className="px-2 py-1 border-b border-[var(--border)]">
                  {row[c] == null ? '—' : typeof row[c] === 'object' ? JSON.stringify(row[c]) : String(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 25 && (
        <div className="text-xs text-[var(--text-dim)] mt-1">…and {rows.length - 25} more</div>
      )}
    </div>
  )
}

// A single horizontal gauge: value against min..max with warn/danger
// thresholds coloring the fill. Used for occupancy, queue depth, etc.
function GaugeBar({ label, value, view }: { label: string; value: number; view: StateViewSpec }) {
  const min = view.min ?? 0
  const max = view.max ?? 100
  const span = max - min || 1
  const pct = Math.max(0, Math.min(100, ((value - min) / span) * 100))
  const danger = view.danger != null && value >= view.danger
  const warn = !danger && view.warn != null && value >= view.warn
  const color = danger ? 'var(--danger,#ef4444)' : warn ? 'var(--warning,#f59e0b)' : 'var(--success,#22c55e)'
  return (
    <div title={view.description || view.name}>
      <div className="flex items-baseline justify-between text-xs mb-1">
        <span className="text-[var(--text-dim)]">{label}</span>
        <span className="font-semibold tabular-nums">
          {value}
          {view.unit ? <span className="text-[var(--text-dim)] font-normal"> {view.unit}</span> : null}
        </span>
      </div>
      <div className="h-2 w-full rounded-full bg-[var(--bg-2)] overflow-hidden">
        <div className="h-full rounded-full transition-all" style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  )
}

function GaugeView({ view, value }: { view: StateViewSpec; value: any }) {
  // Scalar → one gauge; dict-of-numbers → one gauge per key (per camera/zone).
  const entries: [string, number][] =
    value != null && typeof value === 'object' && !Array.isArray(value)
      ? Object.entries(value)
          .filter(([, v]) => typeof v === 'number')
          .map(([k, v]) => [k, v as number])
      : typeof value === 'number'
        ? [[view.label, value]]
        : []
  if (entries.length === 0) return null
  const single = entries.length === 1 && entries[0][0] === view.label
  return (
    <div title={view.description || view.name}>
      {!single && <div className="text-xs text-[var(--text-dim)] mb-1">{view.label}</div>}
      <div className="space-y-2">
        {entries.map(([k, v]) => (
          <GaugeBar key={k} label={k} value={v} view={view} />
        ))}
      </div>
    </div>
  )
}

function fmtWhen(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'number') {
    // epoch seconds or ms → relative age
    const now = Date.now() / 1000
    const t = v > 1e12 ? v / 1000 : v
    const age = Math.max(0, now - t)
    if (age < 60) return `${Math.floor(age)}s ago`
    if (age < 3600) return `${Math.floor(age / 60)}m ago`
    if (age < 86400) return `${Math.floor(age / 3600)}h ago`
    return `${Math.floor(age / 86400)}d ago`
  }
  return String(v)
}

function LogView({ view, value }: { view: StateViewSpec; value: any }) {
  // A recent-events feed: array of strings or {message/text, time/ts, level}.
  if (!Array.isArray(value) || value.length === 0) return null
  const limit = view.limit ?? 12
  const rows = value.slice(-limit).reverse()
  return (
    <div title={view.description || view.name}>
      <div className="text-xs text-[var(--text-dim)] mb-1">{view.label}</div>
      <div className="rounded border border-[var(--border)] bg-[var(--bg-2)] divide-y divide-[var(--border)] max-h-64 overflow-y-auto">
        {rows.map((row, i) => {
          const isObj = row != null && typeof row === 'object'
          const msg = isObj ? row.message ?? row.text ?? row.msg ?? JSON.stringify(row) : String(row)
          const when = isObj ? fmtWhen(row.time ?? row.ts ?? row.timestamp ?? row.at) : ''
          const level = isObj ? String(row.level ?? row.severity ?? '') : ''
          const dot = level === 'high' || level === 'error' ? 'var(--danger,#ef4444)'
            : level === 'warn' || level === 'warning' ? 'var(--warning,#f59e0b)'
            : 'var(--text-dim)'
          return (
            <div key={i} className="flex items-start gap-2 px-2 py-1.5 text-xs">
              <span className="mt-1 h-1.5 w-1.5 rounded-full shrink-0" style={{ background: dot }} />
              <span className="flex-1 break-words">{msg}</span>
              {when && <span className="text-[var(--text-dim)] shrink-0 tabular-nums">{when}</span>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function GalleryView({ view, value }: { view: StateViewSpec; value: any }) {
  // A thumbnail wall: array of {image|url|src (data-uri or path), label, time}.
  // For LPR plate crops, package-on-porch snapshots, doorbell faces.
  if (!Array.isArray(value) || value.length === 0) return null
  const limit = view.limit ?? 12
  const items = value.slice(-limit).reverse()
  return (
    <div title={view.description || view.name}>
      <div className="text-xs text-[var(--text-dim)] mb-1">{view.label}</div>
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2">
        {items.map((it, i) => {
          const isObj = it != null && typeof it === 'object'
          const src = isObj ? it.image ?? it.url ?? it.src ?? it.thumbnail : typeof it === 'string' ? it : undefined
          const caption = isObj ? it.label ?? it.plate ?? it.name ?? it.caption : undefined
          const when = isObj ? fmtWhen(it.time ?? it.ts ?? it.timestamp ?? it.at) : ''
          return (
            <div key={i} className="rounded border border-[var(--border)] bg-[var(--bg-2)] overflow-hidden">
              {src ? (
                <img src={src} alt={caption || 'snapshot'} className="w-full h-24 object-cover" loading="lazy" />
              ) : (
                <div className="w-full h-24 grid place-items-center text-[var(--text-dim)] text-xs">no image</div>
              )}
              {(caption || when) && (
                <div className="px-2 py-1 text-xs flex items-center justify-between gap-1">
                  <span className="truncate font-medium">{caption}</span>
                  {when && <span className="text-[var(--text-dim)] shrink-0">{when}</span>}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

export function LiveStateViews({ appId, views }: { appId: string; views: StateViewSpec[] }) {
  // Same key as AppStatusChip; enabled:false — this component only
  // OBSERVES the cache the chip's "Check" fills (no extra fan-out).
  const statusQuery = useQuery({
    queryKey: ['app-status', appId],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(appId)
      return data as AppStatusResp
    },
    enabled: false,
    retry: 0,
  })
  const state = statusQuery.data?.state
  if (state == null) return null
  const metrics = views.filter((v) => v.kind === 'metric')
  const gauges = views.filter((v) => v.kind === 'gauge')
  // Tables, logs and galleries are full-width blocks rendered in the
  // author's declared order (so a manifest can interleave them).
  const blocks = views.filter((v) => v.kind === 'table' || v.kind === 'log' || v.kind === 'gallery')
  return (
    <div className="space-y-3 pt-1">
      {metrics.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {metrics.map((v) => (
            <MetricView key={v.name} view={v} value={getStatePath(state, v.path)} />
          ))}
        </div>
      )}
      {gauges.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-3">
          {gauges.map((v) => (
            <GaugeView key={v.name} view={v} value={getStatePath(state, v.path)} />
          ))}
        </div>
      )}
      {blocks.map((v) =>
        v.kind === 'table' ? (
          <TableView key={v.name} view={v} value={getStatePath(state, v.path)} />
        ) : v.kind === 'log' ? (
          <LogView key={v.name} view={v} value={getStatePath(state, v.path)} />
        ) : (
          <GalleryView key={v.name} view={v} value={getStatePath(state, v.path)} />
        )
      )}
    </div>
  )
}

/* ---------------------------- App card --------------------------- */

// The registry's lifecycle vocabulary → badge colors. Distinct from
// statusVariant (health strings): dormant is a normal resting state, not
// a warning.
function skillStatusVariant(status: SkillEntry['status']): BadgeVariant {
  switch (status) {
    case 'active':
    case 'available':
      return 'success'
    case 'degraded':
      return 'warning'
    case 'missing-dependency':
      return 'destructive'
    default:
      return 'neutral'
  }
}

/** The licence line on a licensed app's card: status, plan, expiry, the
 * app's own message; and, for administrators, the key entry itself. The
 * key is sent once and never read back. */
function LicensePanel({ app, isAdmin }: { app: RegisteredApp; isAdmin: boolean }) {
  const queryClient = useQueryClient()
  const { showSuccess, showError } = useSnackbar()
  const [key, setKey] = useState('')
  const ent = app.entitlement
  const status = ent?.status ?? 'none'
  const variant: BadgeVariant =
    status === 'valid' ? 'success' : status === 'invalid' ? 'destructive' : 'warning'
  const label =
    status === 'valid'
      ? `licensed${ent?.plan ? ` · ${ent.plan}` : ''}${ent?.expires_at ? ` · until ${ent.expires_at.slice(0, 10)}` : ''}`
      : status === 'invalid'
        ? 'licence rejected'
        : status === 'unverified'
          ? 'licence not yet verified'
          : 'licence required'
  const set = useMutation({
    mutationFn: () => apiService.setAppLicense(app.id, key.trim()),
    onSuccess: (res: any) => {
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      setKey('')
      const e = res?.data?.entitlement
      if (e?.status === 'valid') showSuccess(`${app.name}: licence accepted`)
      else showError(`${app.name}: ${e?.message || 'licence not accepted'}`)
    },
    onError: (e) => showError(extractApiError(e, 'Could not store the licence key.')),
  })
  const verify = useMutation({
    mutationFn: () => apiService.verifyAppLicense(app.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['apps'] }),
    onError: (e) => showError(extractApiError(e, 'Could not re-verify the licence.')),
  })
  const clear = useMutation({
    mutationFn: () => apiService.clearAppLicense(app.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['apps'] }),
    onError: (e) => showError(extractApiError(e, 'Could not clear the licence key.')),
  })
  return (
    <div className="rounded border border-[var(--border)] p-2 space-y-2">
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <Badge variant={variant}>{label}</Badge>
        {ent?.message && status !== 'valid' && (
          <span className="text-[var(--text-dim)]">{ent.message}</span>
        )}
        {ent?.limits && Object.keys(ent.limits).length > 0 && (
          <span className="text-[var(--text-dim)]">
            {Object.entries(ent.limits).map(([k, v]) => `${k}: ${String(v)}`).join(' · ')}
          </span>
        )}
      </div>
      {isAdmin && (
        <div className="flex items-center gap-2">
          <input
            className="flex-1 px-2 py-1 text-sm font-mono rounded border border-[var(--border)] bg-[var(--bg-2)] text-[var(--text)]"
            placeholder={ent?.has_license_key ? 'replace licence key…' : 'licence key'}
            value={key}
            onChange={(e) => setKey(e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
          <Button variant="outline" onClick={() => set.mutate()} disabled={!key.trim() || set.isPending}>
            {set.isPending ? 'Verifying…' : 'Save & verify'}
          </Button>
          {ent?.has_license_key && (
            <>
              <Button variant="ghost" onClick={() => verify.mutate()} disabled={verify.isPending}>Re-check</Button>
              <Button variant="ghost" onClick={() => clear.mutate()} disabled={clear.isPending}>Forget</Button>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function AppCard({ app, tasks, skill, onConfigure }: { app: RegisteredApp; tasks: Set<string>; skill?: SkillEntry; onConfigure: () => void }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccess, showError } = useSnackbar()
  // Enable/disable is a site decision (superuser-only on the server;
  // uninstall keeps its own apps.install permission); everyone can open
  // Configure for their cameras' zones.
  const { user: me } = useAuth()
  const isAdmin = !!me?.is_superuser
  const [uninstallPollActive, setUninstallPollActive] = useState(false)
  const [uninstallNote, setUninstallNote] = useState<string | null>(null)
  // Manifest-declared operator action currently open in its form modal.
  const [activeAction, setActiveAction] = useState<ManifestAction | null>(null)

  const requires = asStringList(app.manifest?.requires_tasks)
  const missing = requires.filter((t) => !tasks.has(t))
  const manifestActions = (app.manifest?.actions ?? []).filter((a) => a && a.name && a.label)

  const toggleMutation = useMutation({
    mutationFn: () => (app.enabled ? apiService.disableApp(app.id) : apiService.enableApp(app.id)),
    onSuccess: () => {
      const wasEnabling = !app.enabled
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      showSuccess(`${app.name} ${app.enabled ? 'disabled' : 'enabled'}`)
      // First enable takes the operator straight to the app's own page so
      // they land on its live dashboard and actions, not back on the grid.
      if (wasEnabling) navigate(`/app-catalog/${app.id}`)
    },
    onError: (e) => showError(extractApiError(e, `Failed to ${app.enabled ? 'disable' : 'enable'} ${app.name}.`)),
  })

  const uninstallStatus = useInstallStatusPoll(app.id, uninstallPollActive)
  // A licensed app cannot be enabled until the app has accepted a key.
  const needsLicense =
    app.manifest?.entitlement === 'license_key' && app.entitlement?.status !== 'valid'

  // Same opt-in + RBAC gating as install: a 403 degrades to an inline note
  // (fail-closed) rather than an error toast, since the operator may have
  // one-click disabled and expects to run the removal command by hand.
  const uninstallMutation = useMutation({
    mutationFn: () => apiService.uninstallApp(app.id),
    onMutate: () => setUninstallNote(null),
    onSuccess: () => {
      showSuccess(`Uninstall requested — ${app.name} will be removed once the reconciler applies it`)
      setUninstallPollActive(true)
    },
    onError: (e) => {
      if (isForbidden(e)) {
        setUninstallNote(
          extractApiError(
            e,
            "One-click uninstall is disabled by your operator (or you don't have permission)."
          )
        )
      } else {
        setUninstallNote(extractApiError(e, `Failed to request uninstall of ${app.name}.`))
      }
    },
  })

  // timedOut releases the button (installer may not be running) — same
  // anti-wedge as the install modal.
  const uninstallInFlight =
    uninstallMutation.isPending ||
    (uninstallStatus.data?.status === 'pending' && !uninstallStatus.timedOut)

  const confirmUninstall = () => {
    if (window.confirm(`Uninstall ${app.name}? This asks the reconciler to remove the app from this host.`)) {
      uninstallMutation.mutate()
    }
  }

  return (
    <Card>
      <CardHeader>
        <Boxes size={16} className="text-[var(--text-dim)]" />
        <Link to={`/app-catalog/${app.id}`} className="hover:underline">
          <CardTitle>{app.name}</CardTitle>
        </Link>
        <Badge variant="info">{app.category}</Badge>
        <span className="text-xs text-[var(--text-dim)]">v{app.version}</span>
        <PricingBadge pricing={app.manifest?.pricing} note={app.manifest?.price_note} />
        <div className="ml-auto">
          <AppStatusChip appId={app.id} />
        </div>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="text-[var(--text-dim)]">{app.manifest?.summary || 'No summary provided.'}</div>

        {app.manifest?.entitlement === 'license_key' && (
          <LicensePanel app={app} isAdmin={isAdmin} />
        )}

        {/* RFC-0002 Phase 1: the skill this app provides, as the platform
            registry sees it — same derivation the agent's panel renders,
            so the two consumers can never tell a different story. */}
        {skill && (
          <div className="flex items-center gap-2 flex-wrap text-xs">
            <Badge variant={skillStatusVariant(skill.status)}>
              skill: {skill.status}
              {skill.reason ? ` — ${skill.reason}` : ''}
            </Badge>
            {skill.publishes.length > 0 && (
              <span className="text-[var(--text-dim)]">
                publishes {skill.publishes.join(', ')}
              </span>
            )}
          </div>
        )}

        {requires.length > 0 && (
          <div>
            {missing.length === 0 ? (
              <Badge variant="success">● requires {requires.join(' + ')} — available</Badge>
            ) : (
              <Badge variant="warning">requires {missing.join(' + ')} — not installed</Badge>
            )}
          </div>
        )}

        {Array.isArray(app.manifest?.state_schema) && app.manifest.state_schema.length > 0 && (
          <LiveStateViews appId={app.id} views={app.manifest.state_schema as StateViewSpec[]} />
        )}

        <div className="flex items-center gap-2 pt-1">
          {isAdmin && (
          <Button
            variant={app.enabled ? 'default' : 'primary'}
            onClick={() => toggleMutation.mutate()}
            disabled={toggleMutation.isPending || (!app.enabled && needsLicense)}
            title={!app.enabled && needsLicense ? 'Enter a licence key the app accepts first' : undefined}
            aria-pressed={app.enabled}
          >
            {toggleMutation.isPending ? 'Working…' : app.enabled ? 'Disable' : 'Enable'}
          </Button>
          )}
          <Button variant="outline" onClick={onConfigure}>
            <Settings2 size={14} /> Configure
          </Button>
          {manifestActions.map((a) => (
            <Button
              key={a.name}
              variant="outline"
              onClick={() => setActiveAction(a)}
              disabled={!app.enabled}
              title={a.description || a.name}
            >
              {a.label}
            </Button>
          ))}
          <Button variant="danger" onClick={confirmUninstall} disabled={uninstallInFlight}>
            <Trash2 size={14} /> {uninstallInFlight ? 'Uninstalling…' : 'Uninstall'}
          </Button>
          <Badge variant={app.enabled ? 'success' : 'neutral'} className="ml-auto">
            {app.enabled ? 'enabled' : 'disabled'}
          </Badge>
        </div>

        {uninstallNote && <div className="text-sm text-amber-400">{uninstallNote}</div>}
        {uninstallPollActive && uninstallStatus.data && <InstallStatusNote status={uninstallStatus.data} />}
        {uninstallStatus.timedOut && (
          <div className="text-xs text-amber-400">
            Still pending after 5 minutes — the app-installer service may not be
            running (see docs/APPS_INSTALL.md).
          </div>
        )}
      </CardContent>
      {activeAction && (
        <AppActionModal app={app} action={activeAction} onClose={() => setActiveAction(null)} />
      )}
    </Card>
  )
}

/* -------------------------- Install modal ------------------------ */

// The command/compose display is the ALWAYS-PRESENT fallback (the fail-closed
// path): install may be disabled or unpermitted, so we never assume one-click
// works. When the backend opts in AND the caller is permitted, the primary
// "Install (one-click)" button POSTs an intent and we poll the reconciler; a
// 403 quietly degrades to "run the command below instead."
function InstallModal({ app, onClose }: { app: IndexApp; onClose: () => void }) {
  const { showSuccess, showError } = useSnackbar()
  const [copied, setCopied] = useState<string | null>(null)
  const [pollActive, setPollActive] = useState(false)
  const [forbidden, setForbidden] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const statusQuery = useInstallStatusPoll(app.id, pollActive)

  const installMutation = useMutation({
    mutationFn: () => apiService.installApp(app.id),
    onMutate: () => {
      setForbidden(null)
      setError(null)
    },
    onSuccess: () => {
      showSuccess('Install requested — the app will appear under Installed once the reconciler applies it')
      setPollActive(true)
    },
    onError: (e) => {
      if (isForbidden(e)) {
        setForbidden(
          extractApiError(
            e,
            "One-click install is disabled by your operator (or you don't have permission). Run the command below instead."
          )
        )
      } else {
        setError(extractApiError(e, 'Failed to request install.'))
      }
    },
  })

  const status = statusQuery.data?.status
  // timedOut releases the button: an intent can sit "pending" forever when
  // the opt-in installer container isn't running — don't wedge the modal.
  const inFlight =
    installMutation.isPending || (status === 'pending' && !statusQuery.timedOut)

  const copy = async (label: string, text: string) => {
    try {
      // navigator.clipboard is unavailable in non-secure (plain-HTTP) or
      // sandboxed contexts — fall back to execCommand so an operator on a
      // LAN http:// deployment can still copy the install command.
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        const ok = document.execCommand('copy')
        document.body.removeChild(ta)
        if (!ok) throw new Error('copy command rejected')
      }
      setCopied(label)
      showSuccess('Copied to clipboard')
      window.setTimeout(() => setCopied((c) => (c === label ? null : c)), 1500)
    } catch {
      showError('Copy failed — select the text and copy manually')
    }
  }

  const command = app.install?.command?.trim()
  const compose = app.install?.compose?.trim()

  return (
    <Modal open title={`Install ${app.name}`} onClose={onClose} widthClassName="w-[640px]">
      <div className="space-y-4">
        {/* Primary path: one-click install (opt-in + RBAC gated server-side). */}
        <div className="rounded border border-[var(--border)] bg-[var(--bg-2)] p-3 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="primary" onClick={() => installMutation.mutate()} disabled={inFlight}>
              <Download size={14} /> {inFlight ? 'Installing…' : 'Install (one-click)'}
            </Button>
            <span className="text-xs text-[var(--text-dim)]">
              The backend enforces opt-in and permissions; if it's disabled, use the command below.
            </span>
          </div>
          {forbidden && <div className="text-sm text-amber-400">{forbidden}</div>}
          {error && <div className="text-sm text-red-400">{error}</div>}
          {pollActive && statusQuery.data && <InstallStatusNote status={statusQuery.data} />}
          {statusQuery.timedOut && (
            <div className="text-sm text-amber-400">
              Still pending after 5 minutes — the app-installer service may not be
              running. Start it with the app-installer compose profile (see
              docs/APPS_INSTALL.md), or use the command below.
            </div>
          )}
        </div>

        <div className="text-sm text-[var(--text-dim)]">
          Or run this on your OpenNVR host, then the app self-registers and appears under Installed.
        </div>

        {command && (
          <div>
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs font-medium text-[var(--text-dim)]">Command</span>
              <Button variant="ghost" className="text-xs px-2 py-1" onClick={() => copy('command', command)}>
                {copied === 'command' ? <Check size={12} /> : <Copy size={12} />} Copy
              </Button>
            </div>
            <pre className="w-full overflow-x-auto rounded border border-[var(--border)] bg-[var(--bg-2)] p-3 text-xs text-[var(--text)] font-mono whitespace-pre-wrap">
              {command}
            </pre>
          </div>
        )}

        {compose && (
          <div>
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs font-medium text-[var(--text-dim)]">docker-compose.yml</span>
              <Button variant="ghost" className="text-xs px-2 py-1" onClick={() => copy('compose', compose)}>
                {copied === 'compose' ? <Check size={12} /> : <Copy size={12} />} Copy
              </Button>
            </div>
            <pre className="w-full max-h-72 overflow-auto rounded border border-[var(--border)] bg-[var(--bg-2)] p-3 text-xs text-[var(--text)] font-mono">
              {compose}
            </pre>
          </div>
        )}

        {!command && !compose && (
          <div className="text-sm text-[var(--text-dim)]">This entry provides no install instructions.</div>
        )}
      </div>

      <div className="mt-4 flex justify-end">
        <Button variant="primary" onClick={onClose}>Done</Button>
      </div>
    </Modal>
  )
}

/* ------------------------ Available app card --------------------- */

function AvailableAppCard({ app, tasks, onInstall }: { app: IndexApp; tasks: Set<string>; onInstall: () => void }) {
  const requires = asStringList(app.requires_tasks)
  const missing = requires.filter((t) => !tasks.has(t))

  return (
    <Card>
      <CardHeader>
        <Boxes size={16} className="text-[var(--text-dim)]" />
        <CardTitle>{app.name}</CardTitle>
        <Badge variant="info">{app.category}</Badge>
        <span className="text-xs text-[var(--text-dim)]">v{app.version}</span>
        <PricingBadge pricing={app.pricing} note={app.price_note} />
        <LicenceRequiredBadge entitlement={app.entitlement} />
        {app.kind === 'external' && <Badge variant="neutral">third-party</Badge>}
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="text-[var(--text-dim)]">{app.summary || 'No summary provided.'}</div>
        {(app.author || app.verified) && (
          <div className="flex items-center gap-2 text-xs text-[var(--text-dim)]">
            {app.author && <span>by {app.author}</span>}
            <VerifiedBadge verified={app.verified} author={app.author} />
          </div>
        )}
        {app.entitlement === 'license_key' && (
          <div className="text-xs text-[var(--text-dim)]">
            Licensed app: after install, an administrator enters the vendor's key in the
            catalog; the app cannot be enabled until it accepts one.
          </div>
        )}

        {requires.length > 0 && (
          <div>
            {missing.length === 0 ? (
              <Badge variant="success">● requires {requires.join(' + ')} — available</Badge>
            ) : (
              <Badge variant="warning">requires {missing.join(' + ')} — not installed</Badge>
            )}
          </div>
        )}

        <div className="flex items-center gap-2 pt-1">
          {app.kind === 'external' && app.external_url ? (
            <a href={app.external_url} target="_blank" rel="noreferrer">
              <Button variant="primary">
                <ExternalLink size={14} /> Learn more
              </Button>
            </a>
          ) : (
            <Button variant="primary" onClick={onInstall}>
              <Download size={14} /> Install
            </Button>
          )}
          {app.docs_url && (
            <a
              href={app.docs_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-sm text-[var(--text-dim)] hover:text-[var(--text)]"
            >
              <ExternalLink size={14} /> Docs
            </a>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

/* ----------------------------- View ------------------------------ */

function SkeletonGrid({ count = 3 }: { count?: number }) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-3">
      {Array.from({ length: count }).map((_, i) => (
        <Skeleton key={i} className="h-44" />
      ))}
    </div>
  )
}

function GroupHeader({ title, count }: { title: string; count?: number }) {
  return (
    <h3 className="text-sm font-semibold text-[var(--text)] tracking-wide">
      {title}
      {typeof count === 'number' && count > 0 && (
        <span className="ml-2 text-xs font-normal text-[var(--text-dim)]">{count}</span>
      )}
    </h3>
  )
}

export function AppCatalog() {
  const appsQuery = useApps()
  const indexQuery = useAppIndex()
  const capsQuery = useKaiCapabilities()
  const skillsQuery = useSkillsRegistry()
  const [configApp, setConfigApp] = useState<RegisteredApp | null>(null)
  const [installApp, setInstallApp] = useState<IndexApp | null>(null)

  const tasks = useMemo(() => availableTasks(capsQuery.data), [capsQuery.data])
  const apps = appsQuery.data ?? []

  // Registry entries for app-provided skills, keyed by app id ("app:<id>"
  // entries carry the bare app id in provider.providers[0]).
  const skillsByApp = useMemo(() => {
    const m = new Map<string, SkillEntry>()
    for (const s of skillsQuery.data ?? []) {
      if (s?.provider?.kind === 'app' && s.provider.providers?.[0]) {
        m.set(s.provider.providers[0], s)
      }
    }
    return m
  }, [skillsQuery.data])

  // Available = index entries not yet installed. Entries with installed=true are
  // already registered and render in the Installed group (deduped by id there).
  const available = useMemo(
    () => (indexQuery.data ?? []).filter((a) => !a.installed),
    [indexQuery.data]
  )
  // Featured = reviewer-flagged listings not yet installed; they render in
  // their own row above the full list (and stay in the list too, so a
  // category scan still finds them).
  const featured = useMemo(() => available.filter((a) => a.featured), [available])

  const refresh = () => {
    appsQuery.refetch()
    indexQuery.refetch()
  }

  return (
    <section className="space-y-6">
      <PageHeader
        title="App Store"
        description="Detector apps built on the OpenNVR App SDK. Enable, configure, and monitor installed apps, or browse the index for more to install — each card checks its required AI tasks against the adapters registered with KAI-C."
        actions={
          <Button onClick={refresh} disabled={appsQuery.isPending || indexQuery.isFetching}>
            <RefreshCw size={14} className={appsQuery.isFetching || indexQuery.isFetching ? 'animate-spin' : ''} /> Refresh
          </Button>
        }
      />

      {/* --------------------------- Installed --------------------------- */}
      <div className="space-y-3">
        <GroupHeader title="Installed" count={apps.length} />
        {appsQuery.isPending ? (
          <SkeletonGrid count={6} />
        ) : appsQuery.isError ? (
          <ErrorCard
            title="App registry unavailable"
            message={extractApiError(appsQuery.error, 'Could not load the app registry.')}
            onRetry={() => appsQuery.refetch()}
          />
        ) : apps.length === 0 ? (
          <EmptyState
            icon={<Boxes size={28} />}
            title="No apps installed yet"
            description="Apps self-register on boot; install one from the index below or see sdk/opennvr-app-sdk to build your own."
          />
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-3">
            {apps.map((app) => (
              <AppCard key={app.id} app={app} tasks={tasks} skill={skillsByApp.get(app.id)} onConfigure={() => setConfigApp(app)} />
            ))}
          </div>
        )}
      </div>

      {/* ---------------------- Available to install --------------------- */}
      {/* Best-effort: if the index endpoint errors, we simply omit this group
          rather than blanking the page above. */}
      {!indexQuery.isError && featured.length > 0 && (
        <div className="space-y-3">
          <GroupHeader title="Featured" count={featured.length} />
          <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-3">
            {featured.map((app) => (
              <AvailableAppCard key={`featured-${app.id}`} app={app} tasks={tasks} onInstall={() => setInstallApp(app)} />
            ))}
          </div>
        </div>
      )}

      {!indexQuery.isError && (
        <div className="space-y-3">
          <GroupHeader title="Available to install" count={available.length} />
          {indexQuery.isPending ? (
            <SkeletonGrid count={3} />
          ) : available.length === 0 ? (
            <EmptyState
              icon={<Boxes size={28} />}
              title="No additional apps available"
              description="Every app in the index is already installed."
            />
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-3">
              {available.map((app) => (
                <AvailableAppCard key={app.id} app={app} tasks={tasks} onInstall={() => setInstallApp(app)} />
              ))}
            </div>
          )}
        </div>
      )}

      {configApp && <AppConfigModal key={configApp.id} app={configApp} onClose={() => setConfigApp(null)} />}
      {installApp && <InstallModal key={installApp.id} app={installApp} onClose={() => setInstallApp(null)} />}
    </section>
  )
}
