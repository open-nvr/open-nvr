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

import React, { Suspense, lazy } from 'react'
import ReactDOM from 'react-dom/client'
import './index.css'
import { createBrowserRouter, RouterProvider, Navigate, Outlet } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient } from './lib/queries'
import { AppShell } from './shell/AppShell'
import { AuthProvider, useAuth } from './auth/AuthContext'
import { PermissionsProvider } from './hooks/usePermissions'
import { SnackbarProvider } from './components/Snackbar'
import { ErrorBoundary } from './components/ErrorBoundary'
import { Login } from './views/Login'
import { MFASetup } from './views/MFASetup'
import { MFAVerify } from './views/MFAVerify'

// Views are lazy-loaded so each route becomes its own chunk instead of one
// monolithic bundle. Auth/MFA stay eager: they gate first paint.
//
// Importers for the heaviest/most-visited routes are hoisted to named consts
// so the warm-up below can start their chunk download in parallel with the
// auth bootstrap (dynamic import is cached: lazy() reuses the in-flight
// fetch, so nothing is downloaded twice).

// A failed chunk fetch usually means this tab's index.html is from a build
// that was since replaced — the content-hashed chunk files it references no
// longer exist on the server ("Failed to fetch dynamically imported
// module"). One forced reload fetches the current index.html and everything
// resolves; NVR tabs stay open across upgrades, so this happens after every
// update. The sessionStorage guard stops a reload loop when the failure is
// something else (server down, offline) — the second failure surfaces to the
// route error boundary as before.
const CHUNK_RELOAD_FLAG = 'opennvr.chunk-reloaded'
function reloadOnStale<T>(importer: () => Promise<T>): () => Promise<T> {
  return () =>
    importer().then(
      (m) => {
        try { sessionStorage.removeItem(CHUNK_RELOAD_FLAG) } catch { /* private mode */ }
        return m
      },
      (err) => {
        let alreadyReloaded = true
        try {
          alreadyReloaded = sessionStorage.getItem(CHUNK_RELOAD_FLAG) === '1'
          if (!alreadyReloaded) sessionStorage.setItem(CHUNK_RELOAD_FLAG, '1')
        } catch { /* private mode: fall through to the error boundary */ }
        if (alreadyReloaded) throw err
        window.location.reload()
        // The page is tearing down — never settle, so no error UI flashes.
        return new Promise<T>(() => {})
      },
    )
}

const importDashboard = reloadOnStale(() => import('./views/Dashboard'))
const importLiveView = reloadOnStale(() => import('./views/LiveView'))
const importPlaybackView = reloadOnStale(() => import('./views/PlaybackView'))
const importSyncPlayback = reloadOnStale(() => import('./views/SyncPlayback'))

const Dashboard = lazy(() => importDashboard().then((m) => ({ default: m.Dashboard })))
const LiveView = lazy(() => importLiveView().then((m) => ({ default: m.LiveView })))
const PlaybackView = lazy(() => importPlaybackView().then((m) => ({ default: m.PlaybackView })))
const SyncPlayback = lazy(() => importSyncPlayback().then((m) => ({ default: m.SyncPlayback })))
const Cameras = lazy(reloadOnStale(() => import('./views/Cameras').then((m) => ({ default: m.Cameras }))))
const Vehicles = lazy(reloadOnStale(() => import('./views/Vehicles').then((m) => ({ default: m.Vehicles }))))
const Settings = lazy(reloadOnStale(() => import('./views/Settings').then((m) => ({ default: m.Settings }))))
const Events = lazy(reloadOnStale(() => import('./views/Events').then((m) => ({ default: m.Events }))))
const Updates = lazy(reloadOnStale(() => import('./views/Updates').then((m) => ({ default: m.Updates }))))
const Logs = lazy(reloadOnStale(() => import('./views/Logs').then((m) => ({ default: m.Logs }))))
const AIEngine = lazy(reloadOnStale(() => import('./views/AIEngine').then((m) => ({ default: m.AIEngine }))))
const Compliance = lazy(reloadOnStale(() => import('./views/Compliance').then((m) => ({ default: m.Compliance }))))
const AlertsIncidents = lazy(reloadOnStale(() => import('./views/AlertsIncidents').then((m) => ({ default: m.AlertsIncidents }))))
const Integrations = lazy(reloadOnStale(() => import('./views/Integrations').then((m) => ({ default: m.Integrations }))))
const Support = lazy(reloadOnStale(() => import('./views/Support').then((m) => ({ default: m.Support }))))
const AccessControl = lazy(reloadOnStale(() => import('./views/AccessControl').then((m) => ({ default: m.AccessControl }))))
const BYOK = lazy(reloadOnStale(() => import('./views/BYOK').then((m) => ({ default: m.BYOK }))))
const NetworkView = lazy(reloadOnStale(() => import('./views/NetworkView').then((m) => ({ default: m.NetworkView }))))
const FirmwareView = lazy(reloadOnStale(() => import('./views/FirmwareView').then((m) => ({ default: m.FirmwareView }))))
const AIModelsBYOM = lazy(reloadOnStale(() => import('./views/AIModelsBYOM').then((m) => ({ default: m.AIModelsBYOM }))))
const AIDetectionResults = lazy(reloadOnStale(() => import('./views/AIDetectionResults').then((m) => ({ default: m.AIDetectionResults }))))
const AIAdapters = lazy(reloadOnStale(() => import('./views/AIAdapters').then((m) => ({ default: m.AIAdapters }))))
const AppCatalog = lazy(reloadOnStale(() => import('./views/AppCatalog').then((m) => ({ default: m.AppCatalog }))))
const AppView = lazy(reloadOnStale(() => import('./views/AppView').then((m) => ({ default: m.AppView }))))
const Cloud = lazy(reloadOnStale(() => import('./views/Cloud').then((m) => ({ default: m.Cloud }))))
const OnvifTools = lazy(reloadOnStale(() => import('./views/OnvifTools').then((m) => ({ default: m.OnvifTools }))))
const Register = lazy(reloadOnStale(() => import('./views/Register').then((m) => ({ default: m.Register }))))
const FirstTimeSetup = lazy(reloadOnStale(() => import('./views/FirstTimeSetup').then((m) => ({ default: m.FirstTimeSetup }))))

// Warm the chunk for the route the user is actually loading, immediately at
// module evaluation — long before auth resolves and the router mounts. Then
// idle-prefetch the other top routes so in-app navigation is instant.
const routeWarmups: Array<[RegExp, () => Promise<unknown>]> = [
  [/^\/playback\/sync/, importSyncPlayback],
  [/^\/playback/, importPlaybackView],
  [/^\/live/, importLiveView],
  [/^\/$/, importDashboard],
]
try {
  const path = window.location.pathname
  const match = routeWarmups.find(([re]) => re.test(path))
  if (match) match[1]().catch(() => {})
  const idle = (window as any).requestIdleCallback ?? ((fn: () => void) => setTimeout(fn, 2000))
  idle(() => {
    for (const [, importer] of routeWarmups) importer().catch(() => {})
  })
} catch {
  /* warm-up is best-effort */
}

function RouteFallback() {
  return <div className="p-4 text-sm text-[var(--text-dim)]">Loading…</div>
}

// For lazy routes outside the protected tree (which get SuspenseOutlet).
function lazyRoute(element: React.ReactNode) {
  return <Suspense fallback={<RouteFallback />}>{element}</Suspense>
}

function ProtectedShell() {
  const { user, loading } = useAuth()
  if (loading) return <div className="p-4 text-sm">Loading…</div>
  if (!user) return <Login />
  if (!user.mfa_enabled) return <MFASetup />
  return <AppShell />
}

function SuspenseOutlet() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Outlet />
    </Suspense>
  )
}

const router = createBrowserRouter([
  {
    path: '/',
    element: <ProtectedShell />,
    children: [
      {
        element: <SuspenseOutlet />,
        children: [
          { index: true, element: <Dashboard /> },
          { path: 'live', element: <LiveView /> },
          { path: 'playback', element: <PlaybackView /> },
          { path: 'playback/sync', element: <SyncPlayback /> },
          { path: 'cameras', element: <Cameras /> },
          { path: 'vehicles', element: <Vehicles /> },
          { path: 'rbac/*', element: <AccessControl /> },
          { path: 'byok', element: <BYOK /> },
          { path: 'network/*', element: <NetworkView /> },
          { path: 'firmware', element: <FirmwareView /> },
          { path: 'updates', element: <Updates /> },
          { path: 'logs', element: <Logs /> },
          { path: 'ai-engine', element: <AIEngine /> },
          { path: 'byom', element: <AIModelsBYOM /> },
          { path: 'ai-detection-results', element: <AIDetectionResults /> },
          { path: 'ai-adapters', element: <AIAdapters /> },
          { path: 'app-catalog', element: <AppCatalog /> },
          { path: 'app-catalog/:appId', element: <AppView /> },
          { path: 'compliance', element: <Compliance /> },
          { path: 'alerts-incidents', element: <AlertsIncidents /> },
          { path: 'integrations', element: <Integrations /> },
          { path: 'onvif-tools', element: <OnvifTools /> },
          { path: 'cloud', element: <Cloud /> },
          { path: 'support', element: <Support /> },
          { path: 'settings/*', element: <Settings /> },
          { path: 'audit-logs', element: <Events /> },
          { path: 'events', element: <Navigate to="/audit-logs" replace /> },
        ],
      },
    ],
  },
  { path: '/login', element: <Login /> },
  { path: '/first-time-setup', element: lazyRoute(<FirstTimeSetup />) },
  { path: '/register', element: lazyRoute(<Register />) },
  { path: '/mfa-setup', element: <MFASetup /> },
  { path: '/mfa-verify', element: <MFAVerify /> },
])

ReactDOM.createRoot(document.getElementById('root')!).render(
  <ErrorBoundary title="OpenNVR hit an unexpected error">
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <PermissionsProvider>
          <SnackbarProvider>
            <RouterProvider router={router} />
          </SnackbarProvider>
        </PermissionsProvider>
      </AuthProvider>
    </QueryClientProvider>
  </ErrorBoundary>
)

// Service worker: registered manually AFTER window 'load' so registration
// (and Workbox's precache pass) never competes with first-paint requests.
window.addEventListener('load', () => {
  import('virtual:pwa-register')
    .then(({ registerSW }) => registerSW({ immediate: true }))
    .catch(() => {
      /* PWA support unavailable (e.g. dev without plugin) — app works without it */
    })
})

  // Expose a simple navigate function for non-routed components (menu overlay)
  ; (window as any).routerNavigate = (path: string) => {
    try {
      router.navigate(path)
    } catch { }
  }
