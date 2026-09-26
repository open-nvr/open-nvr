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

// The setup affordances every app page owes its operator, in one place so
// no page ships without them and no two pages say it differently:
//
// - AppPageHeader: an app page's header. Title, description, actions —
//   and, directly under the description, the "no camera selected" notice.
//   An app with no cameras does nothing, and every tab of its page looks
//   like a quiet day; the notice sits with the app's name so it is the
//   first thing read, on every tab, in the same words on every app.
// - AppConfigureButton: the header's Configure. The app's form opens over
//   the page, so a camera picked or a zone drawn shows up in the numbers
//   without a trip to the App Catalog and back.

import { useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Camera, CameraOff, PenLine, Settings2 } from 'lucide-react'
import { useAuth } from '../../auth/AuthContext'
import { Button, PageHeader } from '../../components/ui'
import { useTranslation } from '../../i18n'
import { AppConfigModal, type RegisteredApp } from '../AppCatalog'
import { listOf, skillLabel } from '../../lib/skillNames'

/** Does this app work on cameras and have none? Apps that act only on
 *  other apps' alerts (camera_picker false) never do. picked_cameras
 *  counts every camera claimed in the app's name — the catalog's picker
 *  and a page's own claim (Vehicles' gate roles) alike. */
export function appHasNoCameras(app: RegisteredApp | null | undefined): boolean {
  return !!app && app.enabled && app.camera_picker !== false && app.picked_cameras === 0
}

/** The app's config form, opened over the current page. */
function useAppConfig(app: RegisteredApp | null | undefined, camera?: number | string | null) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const modal = open && app ? (
    <AppConfigModal
      key={app.id}
      app={app}
      initialCamera={camera ?? undefined}
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

/** The Configure control, on the app's own page. Site-wide config is
 *  superuser-only on the server, so anyone else gets no button rather
 *  than a form that refuses to save.
 *
 *  Every page used to link to the App Catalog for this — a store page,
 *  to change how a running product works. The app's form now opens over
 *  the page it belongs to, and the same control serves "Draw it" next
 *  to a camera with no zone: `camera` lands the form on that camera's
 *  Set up; `variant="link"` renders it inline in a sentence. */
export function AppConfigureButton({
  app, size, label = 'Configure', icon = 'settings', variant = 'outline', camera, className, title,
}: {
  app: RegisteredApp | null | undefined
  size?: 'sm' | 'md'
  label?: ReactNode
  icon?: 'settings' | 'pen' | 'none'
  variant?: 'outline' | 'primary' | 'default' | 'link'
  /** Open straight onto this camera's Set up (id or "camN" handle). */
  camera?: number | string | null
  className?: string
  title?: string
}) {
  const { user: me } = useAuth()
  const { openConfig, modal } = useAppConfig(app, camera)
  if (!app || !me?.is_superuser) return null
  if (variant === 'link') {
    return (
      <>
        <button type="button" className={className ?? 'text-[var(--accent)] underline'} title={title}
                onClick={openConfig}>
          {label}
        </button>
        {modal}
      </>
    )
  }
  const glyph = icon === 'pen' ? <PenLine size={14} /> : icon === 'settings' ? <Settings2 size={14} /> : null
  return (
    <>
      <Button size={size} variant={variant} className={className} title={title} onClick={openConfig}>
        {glyph}{glyph ? ' ' : null}{label}
      </Button>
      {modal}
    </>
  )
}

/** The "no camera selected" notice, identical on every app page. Renders
 *  nothing while the app has cameras (or never needs any). Administrators
 *  get the button that fixes it; anyone else is told who can. */
export function AppNoCamerasNotice({ app }: { app: RegisteredApp | null | undefined }) {
  const { t } = useTranslation()
  const { user: me } = useAuth()
  const { openConfig, modal } = useAppConfig(app)
  if (!app || !appHasNoCameras(app)) return null
  // Picking cameras is site-wide config, superuser-only like Configure.
  const selectable = !!me?.is_superuser
  const skills = app.skills ?? []
  return (
    <div
      role="status"
      className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border border-[var(--border)]
                 border-l-4 border-l-[var(--badge-warning-text)] bg-[var(--badge-warning-bg)] px-3 py-2 text-sm"
    >
      <CameraOff size={16} className="shrink-0 text-[var(--badge-warning-text)]" aria-hidden />
      <p className="min-w-0 flex-1">
        <span className="font-medium text-[var(--text)]">{t('appSetup.noCameras.title')}</span>{' '}
        <span className="text-[var(--text-dim)]">
          {t(selectable ? 'appSetup.noCameras.body' : 'appSetup.noCameras.askAdmin', { app: app.name })}
          {/* What a pick turns on — so the notice says why it is worth
              doing, and the Cameras card below has nothing left to add. */}
          {skills.length > 0 && <> {t('appSetup.noCameras.skills', { skills: listOf(skills.map(skillLabel)) })}</>}
        </span>
      </p>
      {selectable && (
        <Button variant="outline" size="sm" onClick={openConfig}>
          <Camera size={14} /> {t('appSetup.noCameras.select')}
        </Button>
      )}
      {modal}
    </div>
  )
}

/** An app page's header: PageHeader with the app's camera notice in its
 *  notice slot, so the notice cannot end up anywhere else on the page. */
export function AppPageHeader({ app, title, description, actions }: {
  app: RegisteredApp | null | undefined
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
}) {
  return (
    <PageHeader
      title={title}
      description={description}
      actions={actions}
      notice={<AppNoCamerasNotice app={app} />}
    />
  )
}
