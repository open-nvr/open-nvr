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

// Full-page "app view" — every installed app gets its own page at
// /app-catalog/:appId, so a catalog app feels like a real product with a
// live dashboard, configuration, and one-click actions, all in-shell and
// behind the platform's auth (no per-app ports). The dashboard, config
// form, and action forms are the SAME declarative pieces the catalog
// card uses — this page just gives them room to breathe.

import { HardwareBadge, type HardwareBar } from '../components/HardwareBadge'
import { useCallback, useMemo, useRef, useState, type CSSProperties } from 'react'
import { useTranslation } from '../i18n'
import { useConfirm } from '../components/ui/ConfirmDialog'
import { useParams, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Settings2, Trash2, Activity, ExternalLink, BookOpen } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { useSnackbar } from '../components/Snackbar'
import { availableTasks as computeAvailableTasks, type CapabilitiesLike, type Tier0Like } from '../lib/kaic'
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, ErrorCard, Skeleton } from '../components/ui'
import {
  AppConfigModal,
  AppActionModal,
  LiveStateViews,
  asStringList,
  statusVariant,
  resolveUiUrl,
  type RegisteredApp,
  type AppStatusResp,
  type ManifestAction,
  UninstalledAppPage,
  RemoveByHand,
  isForbidden,
  isNotFound,
} from './AppCatalog'
import { AppNoCamerasNotice } from './apps/AppSetup'
import { skillLabel } from '../lib/skillNames'
import { Switch } from '../components/ui/Switch'
import { AppCamerasCard } from './apps/AppCamerasCard'
import { AppHeader } from './apps/AppHeader'
import { useAppCameras } from './apps/CameraPicker'
import { LiveCameraPanel } from '../components/live/LiveCameraPanel'
import { Modal } from '../components/Modal'

function useApp(appId: string) {
  return useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
    select: (apps) => apps.find((a) => a.id === appId) ?? null,
  })
}

// Live status for THIS app on the shared ['app-status', id] key so the
// header pill AND the dashboard's LiveStateViews render from one fetch.
function useLiveStatus(appId: string, enabled: boolean) {
  return useQuery({
    queryKey: ['app-status', appId],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(appId)
      return data as AppStatusResp
    },
    enabled,
    retry: 0,
    // Every 2 s: the counts sit beside live video, and an app such as
    // Gold Bar Desk updates them about once a second. At 5 s the table
    // visibly trailed the picture it describes.
    refetchInterval: enabled ? 2000 : false,
  })
}

// RFC-0002 Phase 4 app-surface convention: the app's self-contained HTML
// dashboard through core's JWT-gated proxy. The page is a static snapshot
// (no scripts — rendered sandboxed), so we refetch on an interval instead
// of letting the page self-refresh.
function useAppUi(appId: string, enabled: boolean) {
  return useQuery({
    queryKey: ['app-ui', appId],
    queryFn: async () => {
      const { data } = await apiService.getAppUi(appId)
      return typeof data === 'string' ? data : ''
    },
    enabled,
    retry: 0,
    refetchInterval: enabled ? 15000 : false,
  })
}

function useCapabilities() {
  return useQuery({
    queryKey: ['kai-c-capabilities'],
    queryFn: async () => {
      const { data } = await apiService.getCapabilities()
      return data as CapabilitiesLike
    },
    retry: 0,
  })
}

function useTier0() {
  return useQuery({
    queryKey: ['tier0-metrics'],
    queryFn: async () => {
      const { data } = await apiService.getTier0Metrics()
      return data as Tier0Like
    },
    retry: 0,
    staleTime: 30_000,
  })
}

// The live row is split like the guard compliance page's (counts on
// the left, the camera on the right, a handle between them), and
// remembered the same way: per browser, not something the server keeps.
const SPLIT_KEY = 'opennvr.appPage.split'
const POPPED_KEY = 'opennvr.appPage.livePopped'
//: Neither pane may be squeezed to uselessness.
const SPLIT_MIN = 35
const SPLIT_MAX = 80

/** localStorage, which THROWS in private mode rather than returning null. */
function recall(key: string): string | null {
  try { return window.localStorage.getItem(key) } catch { return null }
}
function remember(key: string, value: string): void {
  try { window.localStorage.setItem(key, value) } catch { /* not an error */ }
}

export function AppView() {
  const { appId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { t } = useTranslation()
  const confirm = useConfirm()
  const { showSuccess, showError } = useSnackbar()

  const appQuery = useApp(appId)
  const app = appQuery.data
  const status = useLiveStatus(appId, Boolean(app?.enabled))
  const caps = useCapabilities()
  const tier0 = useTier0()

  const [configOpen, setConfigOpen] = useState(false)
  const [activeAction, setActiveAction] = useState<ManifestAction | null>(null)
  const [detailsOpen, setDetailsOpen] = useState(false)

  const availableTasks = useMemo(
    () => computeAvailableTasks(caps.data, tier0.data),
    [caps.data, tier0.data]
  )

  // "internal" embeds the sandboxed /ui dashboard; "external" means the
  // app is a full application — we link out instead of embedding.
  const uiMode = app?.manifest?.ui_mode === 'external' ? 'external' : 'internal'
  const externalUrl =
    uiMode === 'external' && app?.manifest?.ui_url ? resolveUiUrl(app.manifest.ui_url) : null
  const hasUi = Boolean(app?.manifest?.has_ui) && uiMode === 'internal'
  const appUi = useAppUi(appId, hasUi && Boolean(app?.enabled))
  const requires = asStringList(app?.manifest?.requires_tasks)
  const missing = requires.filter((t) => !availableTasks.has(t))
  const stateSchema = Array.isArray(app?.manifest?.state_schema) ? app!.manifest!.state_schema! : []
  const actions = (app?.manifest?.actions ?? []).filter((a) => a && a.name && a.label)

  // The app's cameras, live. Same query key as the cameras row above it
  // (AppCamerasCard), so this is one fetch, not two. With several picked
  // cameras the panel's title becomes a switcher; the first is the default.
  const picks = useAppCameras(appId, !!app && app.camera_picker !== false)
  const liveCameras = (picks.data?.cameras ?? []).filter((c) => c.picked)
  const [liveChoice, setLiveChoice] = useState<number | null>(null)
  const liveCamera = liveCameras.find((c) => c.id === liveChoice) ?? liveCameras[0] ?? null

  const [split, setSplit] = useState(() => {
    const n = Number(recall(SPLIT_KEY))
    return Number.isFinite(n) && n >= SPLIT_MIN && n <= SPLIT_MAX ? n : 60
  })
  const [popped, setPopped] = useState(() => recall(POPPED_KEY) === 'yes')
  const rowRef = useRef<HTMLDivElement>(null)
  const startDrag = useCallback((down: React.PointerEvent) => {
    const row = rowRef.current
    if (!row) return
    down.preventDefault()
    // Captured on the handle, so a fast drag that outruns the pointer
    // keeps resizing instead of dropping the gesture over the video.
    const handle = down.currentTarget as HTMLElement
    handle.setPointerCapture(down.pointerId)
    const move = (e: PointerEvent) => {
      const box = row.getBoundingClientRect()
      if (box.width <= 0) return
      const pct = ((e.clientX - box.left) / box.width) * 100
      setSplit(Math.round(Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, pct))))
    }
    const up = () => {
      handle.releasePointerCapture(down.pointerId)
      handle.removeEventListener('pointermove', move)
      handle.removeEventListener('pointerup', up)
      setSplit((pct) => { remember(SPLIT_KEY, String(pct)); return pct })
    }
    handle.addEventListener('pointermove', move)
    handle.addEventListener('pointerup', up)
  }, [])

  const toggle = useMutation({
    mutationFn: () => (app?.enabled ? apiService.disableApp(appId) : apiService.enableApp(appId)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      showSuccess(
        app?.enabled
          ? `${app?.name} disabled — it stops watching its cameras within a few seconds. Its settings and camera selection are kept.`
          : `${app?.name} enabled`
      )
    },
    onError: (e) => showError(extractApiError(e, 'Failed to toggle app.')),
  })

  // One-click removal refused (off, not permitted, or not a catalog app):
  // the page shows the command to run instead of a toast that leaves the
  // administrator with nothing to do.
  const [removeNote, setRemoveNote] = useState<string | null>(null)
  const uninstall = useMutation({
    mutationFn: () => apiService.uninstallApp(appId),
    onMutate: () => setRemoveNote(null),
    onSuccess: () => {
      showSuccess(`Uninstall requested for ${app?.name}`)
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      navigate('/app-catalog')
    },
    onError: (e) => {
      if (isForbidden(e)) {
        setRemoveNote(extractApiError(e, "One-click uninstall is disabled by your operator (or you don't have permission)."))
      } else if (isNotFound(e)) {
        setRemoveNote(`One-click removal covers catalog apps only; ${app?.name ?? 'this app'} was installed another way.`)
      } else {
        showError(extractApiError(e, 'Failed to uninstall.'))
      }
    },
  })

  if (appQuery.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }
  if (appQuery.isError) {
    return <ErrorCard message={extractApiError(appQuery.error, 'Could not load this app.')} />
  }
  // Not installed is not an error — it is the listing, which is exactly
  // what someone following a link from the catalog wants to read before
  // deciding. The catalog owns that page; this route just defers to it.
  if (!app) return <UninstalledAppPage appId={appId} />

  const health = status.data?.health?.status ?? (app.enabled ? 'checking…' : 'disabled')
  const uptimeS = status.data?.health?.uptime_s as number | undefined

  return (
    // Bounded to the window on a desktop, like the guard compliance page:
    // the shell's <main> is min-height, so without this the live row
    // would grow to fit and the page would scroll. 5rem = top bar plus
    // the shell's padding.
    <div className="flex flex-col gap-3 lg:h-[calc(100vh-5rem)]">
      <AppHeader
        name={app.name}
        category={app.category}
        version={app.version}
        summary={app.manifest?.summary}
        badges={<>
          <span className="inline-flex items-center gap-1.5 text-xs">
            <Badge variant={statusVariant(health)}>{health === 'ok' ? 'Running' : health}</Badge>
            {uptimeS != null && (
              <span className="text-[var(--text-dim)]">up {formatUptime(uptimeS)}</span>
            )}
            {app.enabled && (
              <button
                className="text-[var(--text-dim)] hover:text-[var(--text)]"
                title="Refresh status"
                aria-label="Refresh status"
                onClick={() => status.refetch()}
              >
                <RefreshCw size={12} className={status.isFetching ? 'animate-spin' : ''} />
              </button>
            )}
          </span>
          {/* Only when something is missing — a "requirement met"
              badge on every healthy app was noise. */}
          {missing.length > 0 && (
            <Badge variant="warning" title="No installed model provides this, so the app cannot do its job yet.">
              Needs {missing.map(skillLabel).join(' + ')} — not available
            </Badge>
          )}
          <HardwareBadge hardware={(app as { hardware?: HardwareBar | null } | undefined)?.hardware} />
        </>}
        actions={<>
          {externalUrl && (
            <Button
              size="sm"
              variant="primary"
              onClick={() => window.open(externalUrl, '_blank', 'noopener,noreferrer')}
              disabled={!app.enabled}
              title={app.enabled ? externalUrl : 'Enable the app first'}
            >
              <ExternalLink size={14} /> Open app
            </Button>
          )}
          <label
            className="inline-flex items-center gap-2 border border-[var(--border)] px-2.5 py-1 text-sm"
            title={app.enabled ? 'Turn the app off. Its settings and cameras are kept.' : 'Turn the app on'}
          >
            <Switch
              checked={app.enabled}
              onChange={async () => {
                // Turning an app off stops its detections and alerts on every
                // camera at once; turning it on needs no second thought.
                if (app.enabled) {
                  const ok = await confirm({
                    title: t('apps.confirmDisableTitle', { name: app.name }),
                    message: t('apps.confirmDisableMessage'),
                    confirmLabel: t('apps.disable'),
                    danger: true,
                  })
                  if (!ok) return
                }
                toggle.mutate()
              }}
              disabled={toggle.isPending}
              label={`${app.name} enabled`}
            />
            <span className={app.enabled ? '' : 'text-[var(--text-dim)]'}>
              {toggle.isPending ? 'Working…' : app.enabled ? 'On' : 'Off'}
            </span>
          </label>
          <Button size="sm" variant="outline" onClick={() => setConfigOpen(true)}>
            <Settings2 size={14} /> Configure
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="hover:!text-[var(--danger,#ef4444)]"
            title="Uninstall"
            aria-label={`Uninstall ${app.name}`}
            onClick={async () => {
              if (await confirm({ title: `Uninstall ${app.name}?`, confirmLabel: 'Uninstall', danger: true })) uninstall.mutate()
            }}
            disabled={uninstall.isPending}
          >
            <Trash2 size={15} />
          </Button>
        </>}
      >
        {/* Same notice, same words as every app page's header. */}
        <AppNoCamerasNotice app={app} />
      </AppHeader>
      {removeNote && <RemoveByHand app={app} reason={removeNote} />}
      {/* The app's cameras and the skills it brings them — on the app's page,
          like every other application page. */}
      <AppCamerasCard app={app} />

      {/* ── Live: the counts and the camera, side by side ───────────
          As on the guard compliance page: this row takes the height the
          rest leaves, so on a desktop the page itself never scrolls —
          long counts scroll inside their own column. On a narrow screen
          the two stack and the page scrolls as usual. The camera panel
          never moves in the tree when it floats out: re-parenting it
          would unmount the player and drop the stream. */}
      <div ref={rowRef} className="flex flex-col gap-3 lg:min-h-0 lg:flex-1 lg:flex-row lg:gap-0"
           style={{ '--split': `${split}%` } as CSSProperties}>
        <div className={`flex min-w-0 flex-col gap-3 lg:min-h-0 lg:overflow-y-auto ${liveCamera && !popped ? 'lg:w-[var(--split)]' : 'w-full'}`}>
          <Card className={hasUi && app.enabled ? 'shrink-0' : 'shrink-0 lg:flex-1'}>
            <CardHeader className="flex-wrap gap-y-2">
              <Activity size={16} className="text-[var(--text-dim)]" />
              <CardTitle>Live</CardTitle>
              {app.enabled && status.isFetching && (
                <span className="text-xs text-[var(--text-dim)]">updating…</span>
              )}
              {/* The app's actions, small, beside the counts they act on;
                  what each one does is in its tooltip. */}
              {actions.length > 0 && (
                <span className="ml-auto flex flex-wrap justify-end gap-1.5">
                  {actions.map((a) => (
                    <button
                      key={a.name}
                      type="button"
                      className="border border-[var(--border)] bg-[var(--bg-2)] px-2 py-0.5 text-xs hover:border-[var(--accent,var(--border))] disabled:opacity-50"
                      onClick={() => setActiveAction(a)}
                      disabled={!app.enabled}
                      title={!app.enabled ? 'Enable the app first' : a.description || a.label}
                    >
                      {a.label}
                    </button>
                  ))}
                </span>
              )}
            </CardHeader>
            <CardContent>
              {!app.enabled ? (
                <div className="text-sm text-[var(--text-dim)] py-6 text-center">
                  Enable this app to see live activity.
                </div>
              ) : stateSchema.length === 0 ? (
                <div className="text-sm text-[var(--text-dim)] py-6 text-center">
                  {status.data ? 'Running — this app exposes no live metrics.' : 'Connecting…'}
                </div>
              ) : (
                <LiveStateViews appId={app.id} views={stateSchema} />
              )}
            </CardContent>
          </Card>
          {hasUi && app.enabled && (
            <Card>
              <CardHeader>
                <CardTitle>App dashboard</CardTitle>
                <span className="ml-auto text-xs text-[var(--text-dim)]">
                  served by the app · refreshes every 15s
                </span>
              </CardHeader>
              <CardContent>
                {appUi.isError ? (
                  <div className="text-sm text-[var(--text-dim)]">
                    The app's dashboard is unreachable right now.
                  </div>
                ) : (
                  <iframe
                    title={`${app.name} dashboard`}
                    // No scripts, no navigation, no same-origin: the page
                    // is app-authored HTML — render it inert.
                    sandbox=""
                    srcDoc={appUi.data ?? ''}
                    className="w-full border border-[var(--border)] bg-white"
                    style={{ height: 360 }}
                  />
                )}
              </CardContent>
            </Card>
          )}
        </div>
        {liveCamera && !popped && (
          <div
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize the counts and the camera"
            onPointerDown={startDrag}
            className="group hidden w-3 shrink-0 cursor-col-resize items-center justify-center lg:flex"
          >
            <div className="h-10 w-[3px] rounded bg-[var(--border)] group-hover:bg-[var(--accent)]" />
          </div>
        )}
        {/* No cameras picked: nothing here — the header's notice says so,
            with the button that fixes it. */}
        {liveCamera && (
          <div className={popped ? 'h-0 w-0' : 'min-h-[300px] min-w-0 lg:w-[calc(100%-var(--split)-0.75rem)]'}>
            <LiveCameraPanel
              layerScope={`app:${app.id}`}
              cameraId={liveCamera.id}
              cameraName={liveCamera.name}
              cameras={liveCameras.map((c) => ({ id: c.id, name: c.name }))}
              onSelectCamera={setLiveChoice}
              overlayEnabled={app.overlay_enabled}
              popped={popped}
              onTogglePop={() => setPopped((v) => {
                remember(POPPED_KEY, v ? 'no' : 'yes')
                return !v
              })}
            />
          </div>
        )}
      </div>

      {/* ── About, in one line: reference facts nobody reads twice
          should not cost a screen. The long description opens on
          request. */}
      <Card className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 px-3 py-1.5 text-xs text-[var(--text-dim)]">
        {(app.manifest?.description || (app.manifest?.use_cases ?? []).length > 0) && (
          <button type="button" onClick={() => setDetailsOpen(true)}
                  className="inline-flex items-center gap-1 text-[var(--accent,var(--text))] hover:underline">
            <BookOpen size={12} /> Details
          </button>
        )}
        {(app.manifest?.emits ?? []).length > 0 && (
          <span className="inline-flex flex-wrap items-center gap-1.5">
            Emits
            {(app.manifest?.emits ?? []).map((e) => (
              <span key={e.name}
                    className={`font-mono text-[11px] ${e.severity === 'high' ? 'text-[var(--danger)]' : 'text-[var(--text)]'}`}
                    title={e.severity ? `${e.severity} severity` : undefined}>
                {e.name}
              </span>
            ))}
          </span>
        )}
        {(app.manifest?.requires_scopes ?? []).length > 0 && (
          <span title={`Granted at registration, audited: ${(app.manifest?.requires_scopes ?? []).join(', ')}`}>
            {(app.manifest?.requires_scopes ?? []).length} event
            {(app.manifest?.requires_scopes ?? []).length === 1 ? ' scope' : ' scopes'}
          </span>
        )}
        {app.manifest?.author && (
          <span>By <span className="text-[var(--text)]">{app.manifest.author}</span></span>
        )}
        {app.manifest?.license && <span>License: {app.manifest.license}</span>}
        {app.manifest?.website && (
          <a href={app.manifest.website} target="_blank" rel="noreferrer noopener"
             className="inline-flex items-center gap-1 text-[var(--accent,var(--text))] hover:underline">
            <ExternalLink size={11} /> Website
          </a>
        )}
        {app.url && (
          <span>Contract: <span className="font-mono">{app.url}</span></span>
        )}
        {app.manifest?.contact && (
          <a href={app.manifest.contact} target="_blank" rel="noreferrer noopener"
             className="ml-auto text-[var(--accent,var(--text))] hover:underline">
            Need SMS/phone alerts, automation, or a custom feature? Contact us →
          </a>
        )}
      </Card>

      {detailsOpen && (
        <Modal open title={`${app.name} — details`} onClose={() => setDetailsOpen(false)} widthClassName="w-[640px]">
          <div className="space-y-3 text-sm">
            {(app.manifest?.description ?? '')
              .split(/\n{2,}/)
              .map((p) => p.trim())
              .filter(Boolean)
              .map((p, i) => (
                <p key={i} className="leading-relaxed">{p}</p>
              ))}
            {(app.manifest?.use_cases ?? []).length > 0 && (
              <div>
                <div className="text-xs text-[var(--text-dim)] mb-1">What you can do with it</div>
                <ul className="list-disc pl-5 space-y-0.5">
                  {(app.manifest?.use_cases ?? []).map((u) => (
                    <li key={u}>{u}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </Modal>
      )}

      {configOpen && <AppConfigModal app={app} onClose={() => setConfigOpen(false)} />}
      {activeAction && (
        <AppActionModal app={app} action={activeAction} onClose={() => setActiveAction(null)} />
      )}
    </div>
  )
}

function formatUptime(seconds: number): string {
  const s = Math.floor(seconds)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}
