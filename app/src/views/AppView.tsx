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

import { useMemo, useState } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Boxes, RefreshCw, Settings2, Trash2, Activity } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { useSnackbar } from '../components/Snackbar'
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, ErrorCard, Skeleton } from '../components/ui'
import {
  AppConfigModal,
  AppActionModal,
  LiveStateViews,
  asStringList,
  statusVariant,
  type RegisteredApp,
  type AppStatusResp,
  type ManifestAction,
} from './AppCatalog'

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
    refetchInterval: enabled ? 5000 : false,
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
      return data as { adapters?: Record<string, { tasks_advertised?: string[] }> }
    },
    retry: 0,
  })
}

export function AppView() {
  const { appId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccess, showError } = useSnackbar()

  const appQuery = useApp(appId)
  const app = appQuery.data
  const status = useLiveStatus(appId, Boolean(app?.enabled))
  const caps = useCapabilities()

  const [configOpen, setConfigOpen] = useState(false)
  const [activeAction, setActiveAction] = useState<ManifestAction | null>(null)

  const availableTasks = useMemo(() => {
    const set = new Set<string>()
    for (const a of Object.values(caps.data?.adapters ?? {})) {
      for (const t of a.tasks_advertised ?? []) set.add(t)
    }
    return set
  }, [caps.data])

  const hasUi = Boolean(app?.manifest?.has_ui)
  const appUi = useAppUi(appId, hasUi && Boolean(app?.enabled))
  const requires = asStringList(app?.manifest?.requires_tasks)
  const missing = requires.filter((t) => !availableTasks.has(t))
  const stateSchema = Array.isArray(app?.manifest?.state_schema) ? app!.manifest!.state_schema! : []
  const actions = (app?.manifest?.actions ?? []).filter((a) => a && a.name && a.label)

  const toggle = useMutation({
    mutationFn: () => (app?.enabled ? apiService.disableApp(appId) : apiService.enableApp(appId)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      showSuccess(`${app?.name} ${app?.enabled ? 'disabled' : 'enabled'}`)
    },
    onError: (e) => showError(extractApiError(e, 'Failed to toggle app.')),
  })

  const uninstall = useMutation({
    mutationFn: () => apiService.uninstallApp(appId),
    onSuccess: () => {
      showSuccess(`Uninstall requested for ${app?.name}`)
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      navigate('/app-catalog')
    },
    onError: (e) => showError(extractApiError(e, 'Failed to uninstall.')),
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
  if (!app) {
    return (
      <div className="space-y-3">
        <Link to="/app-catalog" className="inline-flex items-center gap-1 text-sm text-[var(--text-dim)] hover:text-[var(--text)]">
          <ArrowLeft size={14} /> App Store
        </Link>
        <ErrorCard message={`App "${appId}" is not installed.`} />
      </div>
    )
  }

  const health = status.data?.health?.status ?? (app.enabled ? 'checking…' : 'disabled')
  const uptimeS = status.data?.health?.uptime_s as number | undefined

  return (
    <div className="space-y-5">
      <Link to="/app-catalog" className="inline-flex items-center gap-1 text-sm text-[var(--text-dim)] hover:text-[var(--text)]">
        <ArrowLeft size={14} /> App Store
      </Link>

      {/* ── Hero header ─────────────────────────────────────────── */}
      <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-1)] p-5">
        <div className="flex flex-wrap items-start gap-4">
          <div className="grid place-items-center h-14 w-14 rounded-xl bg-[var(--bg-2)] border border-[var(--border)] shrink-0">
            <Boxes size={26} className="text-[var(--accent,var(--text-dim))]" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold">{app.name}</h1>
              <Badge variant="info">{app.category}</Badge>
              <span className="text-xs text-[var(--text-dim)]">v{app.version}</span>
            </div>
            <p className="text-sm text-[var(--text-dim)] mt-1 max-w-2xl">
              {app.manifest?.summary || 'No summary provided.'}
            </p>
            {requires.length > 0 && (
              <div className="mt-2">
                {missing.length === 0 ? (
                  <Badge variant="success">● requires {requires.join(' + ')} — available</Badge>
                ) : (
                  <Badge variant="warning">requires {missing.join(' + ')} — not installed</Badge>
                )}
              </div>
            )}
          </div>
          <div className="ml-auto flex flex-col items-end gap-2">
            <div className="inline-flex items-center gap-2">
              <Badge variant={statusVariant(health)}>{health}</Badge>
              {app.enabled && (
                <button
                  className="text-[var(--text-dim)] hover:text-[var(--text)]"
                  title="Refresh status"
                  onClick={() => status.refetch()}
                >
                  <RefreshCw size={13} className={status.isFetching ? 'animate-spin' : ''} />
                </button>
              )}
            </div>
            {uptimeS != null && (
              <span className="text-xs text-[var(--text-dim)]">up {formatUptime(uptimeS)}</span>
            )}
            <div className="flex items-center gap-2 pt-1">
              <Button
                variant={app.enabled ? 'default' : 'primary'}
                onClick={() => toggle.mutate()}
                disabled={toggle.isPending}
              >
                {toggle.isPending ? 'Working…' : app.enabled ? 'Disable' : 'Enable'}
              </Button>
              <Button variant="outline" onClick={() => setConfigOpen(true)}>
                <Settings2 size={14} /> Configure
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  if (window.confirm(`Uninstall ${app.name}?`)) uninstall.mutate()
                }}
                disabled={uninstall.isPending}
              >
                <Trash2 size={14} />
              </Button>
            </div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* ── Live dashboard ─────────────────────────────────────── */}
        <div className="lg:col-span-2 space-y-4">
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
                    className="w-full rounded-md border border-[var(--border)] bg-white"
                    style={{ height: 360 }}
                  />
                )}
              </CardContent>
            </Card>
          )}
          <Card>
            <CardHeader>
              <Activity size={16} className="text-[var(--text-dim)]" />
              <CardTitle>Live</CardTitle>
              {app.enabled && status.isFetching && (
                <span className="ml-auto text-xs text-[var(--text-dim)]">updating…</span>
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
        </div>

        {/* ── Actions + About ────────────────────────────────────── */}
        <div className="space-y-4">
          {actions.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Quick actions</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                {actions.map((a) => (
                  <button
                    key={a.name}
                    className="w-full text-left rounded border border-[var(--border)] bg-[var(--bg-2)] px-3 py-2 hover:border-[var(--accent,var(--border))] disabled:opacity-50"
                    onClick={() => setActiveAction(a)}
                    disabled={!app.enabled}
                    title={!app.enabled ? 'Enable the app first' : a.description || a.name}
                  >
                    <div className="text-sm font-medium">{a.label}</div>
                    {a.description && <div className="text-xs text-[var(--text-dim)]">{a.description}</div>}
                  </button>
                ))}
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader>
              <CardTitle>About</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {asStringList(app.manifest?.emits?.map((e) => e.name)).length > 0 && (
                <div>
                  <div className="text-xs text-[var(--text-dim)] mb-1">Emits</div>
                  <div className="flex flex-wrap gap-1">
                    {(app.manifest?.emits ?? []).map((e) => (
                      <Badge key={e.name} variant={e.severity === 'high' ? 'destructive' : 'neutral'}>{e.name}</Badge>
                    ))}
                  </div>
                </div>
              )}
              {(app.manifest?.requires_scopes ?? []).length > 0 && (
                <div>
                  <div className="text-xs text-[var(--text-dim)] mb-1">
                    Event scopes (granted at registration, audited)
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {(app.manifest?.requires_scopes ?? []).map((sc) => (
                      <Badge key={sc} variant="info">{sc}</Badge>
                    ))}
                  </div>
                </div>
              )}
              {app.url && (
                <div className="text-xs text-[var(--text-dim)]">
                  Contract: <span className="font-mono">{app.url}</span>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

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
