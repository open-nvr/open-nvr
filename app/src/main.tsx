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

import React from 'react'
import ReactDOM from 'react-dom/client'
import './index.css'
import { createBrowserRouter, RouterProvider, Navigate } from 'react-router-dom'
import { AppShell } from './shell/AppShell'
import { Dashboard } from './views/Dashboard'
import { LiveView } from './views/LiveView'
import { PlaybackView } from './views/PlaybackView'
import { Cameras } from './views/Cameras'
import { Settings } from './views/Settings'
import { Events } from './views/Events'
import { Updates } from './views/Updates'
import { Logs } from './views/Logs'
import { AIEngine } from './views/AIEngine'
import { Compliance } from './views/Compliance'
import { AlertsIncidents } from './views/AlertsIncidents'
import { Integrations } from './views/Integrations'
import { Support } from './views/Support'
import { AccessControl } from './views/AccessControl'
import { BYOK } from './views/BYOK'
import { NetworkView } from './views/NetworkView'
import { FirmwareView } from './views/FirmwareView'
import { AIModelsBYOM } from './views/AIModelsBYOM'
import { AIDetectionResults } from './views/AIDetectionResults'
import { Cloud } from './views/Cloud'
import { AuthProvider, useAuth } from './auth/AuthContext'
import { PermissionsProvider } from './hooks/usePermissions'
import { SnackbarProvider } from './components/Snackbar'
import { Login } from './views/Login'
import { MFASetup } from './views/MFASetup'
import { MFAVerify } from './views/MFAVerify'
import { Register } from './views/Register'
import { FirstTimeSetup } from './views/FirstTimeSetup'
import { OnvifTools } from './views/OnvifTools'

function ProtectedShell() {
  const { user, loading } = useAuth()
  if (loading) return <div className="p-4 text-sm">Loading…</div>
  if (!user) return <Login />
  if (!user.mfa_enabled) return <MFASetup />
  return <AppShell />
}

const router = createBrowserRouter([
  {
    path: '/',
    element: <ProtectedShell />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: 'live', element: <LiveView /> },
      { path: 'playback', element: <PlaybackView /> },
      { path: 'cameras', element: <Cameras /> },
      { path: 'rbac/*', element: <AccessControl /> },
      { path: 'byok', element: <BYOK /> },
      { path: 'network/*', element: <NetworkView /> },
      { path: 'firmware', element: <FirmwareView /> },
      { path: 'updates', element: <Updates /> },
      { path: 'logs', element: <Logs /> },
      { path: 'ai-engine', element: <AIEngine /> },
      { path: 'byom', element: <AIModelsBYOM /> },
      { path: 'ai-detection-results', element: <AIDetectionResults /> },
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
  { path: '/login', element: <Login /> },
  { path: '/first-time-setup', element: <FirstTimeSetup /> },
  { path: '/register', element: <Register /> },
  { path: '/mfa-setup', element: <MFASetup /> },
  { path: '/mfa-verify', element: <MFAVerify /> },
])

ReactDOM.createRoot(document.getElementById('root')!).render(
  <AuthProvider>
    <PermissionsProvider>
      <SnackbarProvider>
        <RouterProvider router={router} />
      </SnackbarProvider>
    </PermissionsProvider>
  </AuthProvider>
)

  // Service worker registration is handled by vite-plugin-pwa (injectRegister: 'auto')

  // Expose a simple navigate function for non-routed components (menu overlay)
  ; (window as any).routerNavigate = (path: string) => {
    try {
      router.navigate(path)
    } catch { }
  }
