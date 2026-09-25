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

// The two setup affordances every app page owes its operator, in one
// place so no page ships without them:
//
// - Configure, in the page header. The app's form opens over the page,
//   so a camera picked or a zone drawn shows up in the numbers without a
//   trip to the App Catalog and back.
// - A "no cameras" banner above everything else. An app with no cameras
//   does nothing, and every tab of its page looks like a quiet day — so
//   the banner is page-level, not inside whichever tab happens to own
//   camera setup.

import { useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Settings2 } from 'lucide-react'
import { useAuth } from '../../auth/AuthContext'
import { Button } from '../../components/ui'
import { AppConfigModal, type RegisteredApp } from '../AppCatalog'

/** Does this app work on cameras and have none? Apps that act only on
 *  other apps' alerts (camera_picker false) never do. */
export function appHasNoCameras(app: RegisteredApp | null | undefined): boolean {
  return !!app && app.enabled && app.camera_picker !== false && app.picked_cameras === 0
}

/** The app's config form, opened over the current page. */
function useAppConfig(app: RegisteredApp | null | undefined) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const modal = open && app ? (
    <AppConfigModal
      key={app.id}
      app={app}
      onClose={() => {
        setOpen(false)
        // Picks and zones change both the app's row (picked_cameras) and
        // what its status reports — refetch each so the page catches up.
        queryClient.invalidateQueries({ queryKey: ['apps'] })
        queryClient.invalidateQueries({ queryKey: ['app-status', app.id] })
      }}
    />
  ) : null
  return { openConfig: () => setOpen(true), modal }
}

/** Header button. Site-wide config is superuser-only on the server, so
 *  anyone else gets no button rather than a form that refuses to save. */
export function AppConfigureButton({ app, size }: { app: RegisteredApp | null | undefined; size?: 'sm' }) {
  const { user: me } = useAuth()
  const { openConfig, modal } = useAppConfig(app)
  if (!app || !me?.is_superuser) return null
  return (
    <>
      <Button size={size} variant="outline" onClick={openConfig}>
        <Settings2 size={14} /> Configure
      </Button>
      {modal}
    </>
  )
}

/** Page-level warning while the app has no cameras. A page that knows
 *  better passes `when` (Vehicles counts cameras holding the plate-reading
 *  role, not just picks), and `action` to send the operator somewhere
 *  better than the config form (Vehicles: its camera-roles tab). */
export function AppNoCamerasBanner({ app, when, message, action }: {
  app: RegisteredApp | null | undefined
  when?: boolean
  message?: ReactNode
  action?: ReactNode
}) {
  const { user: me } = useAuth()
  const { openConfig, modal } = useAppConfig(app)
  if (!app || !(when ?? appHasNoCameras(app))) return null
  const isAdmin = !!me?.is_superuser
  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-3 rounded border border-[var(--warning,#b7791f)] px-3 py-2 text-sm text-[var(--warning,#b7791f)]"
    >
      <span className="min-w-0 flex-1">
        {message ?? <>No camera is selected for {app.name} — it does nothing until one is.</>}
        {!isAdmin && ' Ask an administrator to select cameras for it.'}
      </span>
      {isAdmin && (action ?? (
        <Button variant="outline" size="sm" onClick={openConfig}>
          Select cameras
        </Button>
      ))}
      {modal}
    </div>
  )
}
