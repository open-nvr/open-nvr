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
// the skills are information, so they read as a quiet sentence rather
// than as chips that look clickable. While the app has no cameras it
// renders nothing — the header's notice (AppNoCamerasNotice) already
// says so, with the button that fixes it.
import { useState } from 'react'
import { Camera as CameraIcon, Info, PenLine } from 'lucide-react'
import { Badge, Card } from '../../components/ui'
import type { RegisteredApp } from '../AppCatalog'
import { AppConfigureButton, appHasNoCameras } from './AppSetup'
import { listOf, skillLabel } from '../../lib/skillNames'
import { useAppCameras } from './CameraPicker'

/** Cameras shown before "+N more" — enough for a typical site, few
 *  enough that a 40-camera app does not turn the row into a wall. */
const SHOWN = 5

export function AppCamerasCard({ app }: { app: RegisteredApp | null | undefined }) {
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
  const pill = 'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs text-[var(--accent)] hover:bg-[var(--panel)]'

  return (
    <Card className="flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-2.5 text-sm">
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
                     title={c.live_online === false ? 'Camera is offline' : c.location ?? undefined}>
                {c.name}
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

      {/* Information, not a control: plain dim text. */}
      {skills.length > 0 && (
        <div className={`ml-auto flex min-w-0 items-center gap-1.5 text-xs ${dim}`}>
          <Info size={13} className="shrink-0" />
          <span>Adds {listOf(skills.map(skillLabel))} to each camera</span>
        </div>
      )}
    </Card>
  )
}
