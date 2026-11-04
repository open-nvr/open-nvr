// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { StrictMode, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { ShieldAlert } from 'lucide-react'
import './index.css'
import { AuthProvider, useAuth } from './auth'
import Layout from './components/Layout'
import { FeedbackProvider } from './components/feedback'
import { Button, Card, Empty, Loading } from './components/ui'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Register from './pages/Register'
import People from './pages/People'
import Groups from './pages/Groups'
import Schedule from './pages/Schedule'
import Attendance from './pages/Attendance'
import SessionPage from './pages/Session'
import Reports from './pages/Reports'
import Settings from './pages/Settings'

function Gate({ children }: { children: ReactNode }) {
  const { status, error, deviceBlocked, can, refresh, signOut } = useAuth()
  if (status === 'loading') return <Loading />
  if (status === 'signed-out') return <Login />
  if (status === 'error') {
    return (
      <div className="mx-auto max-w-lg px-4 py-16">
        <Card>
          <Empty icon={<ShieldAlert className="h-10 w-10" />} title={deviceBlocked ? 'This browser is waiting for approval' : 'OpenNVR did not answer'}>
            <p>
              {deviceBlocked
                ? 'OpenNVR only lets approved browsers in. Ask an administrator to approve this one in OpenNVR → Settings → Firewall, then try again.'
                : error}
            </p>
            <div className="mt-4 flex justify-center gap-2">
              <Button onClick={refresh}>Try again</Button>
              <Button variant="secondary" onClick={signOut}>
                Sign out
              </Button>
            </div>
          </Empty>
        </Card>
      </div>
    )
  }
  if (!can.view) {
    return (
      <Layout>
        <Card>
          <Empty icon={<ShieldAlert className="h-10 w-10" />} title="Attendance is not part of your role">
            Ask an OpenNVR administrator for the attendance.view permission.
          </Empty>
        </Card>
      </Layout>
    )
  }
  return <Layout>{children}</Layout>
}

function App() {
  return (
    <Gate>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/register" element={<Register />} />
        <Route path="/people" element={<People />} />
        <Route path="/groups" element={<Groups />} />
        <Route path="/schedule" element={<Schedule />} />
        <Route path="/attendance" element={<Attendance />} />
        <Route path="/attendance/:id" element={<SessionPage />} />
        <Route path="/reports" element={<Reports />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Gate>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <FeedbackProvider>
        <AuthProvider>
          <App />
        </AuthProvider>
      </FeedbackProvider>
    </BrowserRouter>
  </StrictMode>,
)
