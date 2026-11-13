// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The cameras an app works on and what it runs on them — on the app's
// own page, so an operator never has to open the catalog to answer
// "which cameras is this running on, and what does it do to them".
// Skills follow apps (docs/CAMERA_ASSIGNMENTS.md): what is listed here is
// the model skills a pick of this app puts in the camera's set.
//
// One row, not a card with a header: it is a fact about the app, read at
// a glance. The cameras and the control that changes them sit together;
// the skills sit at the row's end in small text, their names emphasised
// and the words around them dim (not chips, which would look clickable). While the app has no cameras it
// renders nothing — the header's notice (AppNoCamerasNotice) already
// says so, with the button that fixes it.
import { useState, type ReactNode } from 'react'
import { Camera as CameraIcon, Info, PenLine } from 'lucide-react'
import { Badge, Card } from '../../components/ui'
import type { RegisteredApp } from '../AppCatalog'
import { AppConfigureButton, appHasNoCameras } from './AppSetup'
import { skillLabel } from '../../lib/skillNames'
import { useAppCameras } from './CameraPicker'

/** Cameras shown before "+N more" — enough for a typical site, few
 *  enough that a 40-camera app does not turn the row into a wall. */
const SHOWN = 5

export function AppCamerasCard({ app, embedded = false }: {
  app: RegisteredApp | null | undefined
  /** A row inside the page's header panel (AppPageHeader children) rather
   *  than a card of its own. */
  embedded?: boolean
}) {
  const takesPicks = !!app && app.camera_picker !== false
  const picks = useAppCameras(app?.id ?? '', takesPicks)
  const [expanded, setExpanded] = useState(false)
  if (!app || appHasNoCameras(app)) return null
  const skills = app.skills ?? picks.data?.skills ?? []
  const cameras = (picks.data?.cameras ?? []).filter((c) => c.picked)
  const visible = expanded ? cameras : cameras.slice(0, SHOWN)
  const hidden = cameras.length - visible.length
  const dim = 'text-[var(--text-dim)]'
  const canChange = takesPicks && !app.all_cameras
  const pill = 'inline-flex items-center gap-1 px-1.5 py-0.5 text-xs text-[var(--accent)] hover:bg-[var(--panel)]'
  const row = 'flex flex-wrap items-center gap-x-6 gap-y-2 px-3 py-2 text-[13px]'
  const wrap = (children: ReactNode) => embedded
    ? <div className={`border-t border-[var(--border)] ${row}`}>{children}</div>
    : <Card className={row}>{children}</Card>

  return wrap(
    <>
      {/* Cameras and the control that changes them, as one group. */}
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <span className={`inline-flex items-center gap-1.5 ${dim}`}>
          <CameraIcon size={14} />
          {cameras.length > 0 ? `Cameras (${cameras.length})` : 'Cameras'}
        </span>
        {app.all_cameras ? (
          <span title="The system assigns every camera to this app.">All cameras</span>
        ) : !takesPicks ? (
          <span className={dim} title="This app works on other apps’ alerts, not on video.">
            Not needed
          </span>
        ) : picks.isPending ? (
          <span className={dim}>Loading…</span>
        ) : cameras.length === 0 ? (
          <span className="text-[var(--badge-warning-text)]">None selected</span>
        ) : (
          <>
            {visible.map((c) => (
              <Badge key={c.id} variant={c.live_online === false ? 'warning' : 'neutral'}
                     className="max-w-[180px]"
                     title={c.live_online === false ? `${c.name} — camera is offline` : [c.name, c.location].filter(Boolean).join(' — ')}>
                <span className="min-w-0 truncate">{c.name}</span>
              </Badge>
            ))}
            {hidden > 0 && (
              <button type="button" className={pill} onClick={() => setExpanded(true)}
                      title={cameras.slice(SHOWN).map((c) => c.name).join(', ')}>
                +{hidden} more
              </button>
            )}
            {expanded && cameras.length > SHOWN && (
              <button type="button" className={pill} onClick={() => setExpanded(false)}>
                Show less
              </button>
            )}
          </>
        )}
        {canChange && (
          <AppConfigureButton
            app={app}
            variant="link"
            className={pill}
            title="Choose which cameras this app works on"
            label={<><PenLine size={12} /> {cameras.length ? 'Change' : 'Select'}</>}
          />
        )}
      </div>

      {/* What the app runs on its cameras, always in view but small: the
          skill names carry the weight, the words around them stay dim. */}
      {skills.length > 0 && (
        <span className={`ml-auto inline-flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11px] ${dim}`}>
          <Info size={12} className="shrink-0" aria-hidden="true" />
          <span>Adds</span>
          {skills.map((sk, i) => (
            <span key={sk} className="inline-flex items-center gap-1.5">
              {i > 0 && <span aria-hidden="true">·</span>}
              <span className="font-medium text-[var(--text)]">{skillLabel(sk)}</span>
            </span>
          ))}
          <span>to each camera</span>
        </span>
      )}
    </>
  )
}
