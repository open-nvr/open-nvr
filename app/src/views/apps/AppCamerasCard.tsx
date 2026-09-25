// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The cameras an app works on and the skills it brings to them — on the
// app's own page, so an operator never has to open the catalog to answer
// "which cameras is this running on, and what does it do to them".
// Skills follow apps (docs/CAMERA_ASSIGNMENTS.md): the chips here are the
// model skills a pick of this app puts in the camera's set.
import { Camera as CameraIcon, Sparkles } from 'lucide-react'
import { Badge, Card, CardContent, CardHeader, CardTitle } from '../../components/ui'
import type { RegisteredApp } from '../AppCatalog'
import { AppConfigureButton } from './AppSetup'
import { useAppCameras } from './CameraPicker'

export function AppCamerasCard({ app }: { app: RegisteredApp | null | undefined }) {
  const takesPicks = !!app && app.camera_picker !== false
  const picks = useAppCameras(app?.id ?? '', takesPicks)
  if (!app) return null
  const skills = app.skills ?? picks.data?.skills ?? []
  const cameras = (picks.data?.cameras ?? []).filter((c) => c.picked)
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2">
        <CardTitle className="flex items-center gap-2">
          <CameraIcon size={15} /> Cameras &amp; skills
        </CardTitle>
        {takesPicks && <AppConfigureButton app={app} size="sm" label={cameras.length ? 'Change cameras' : 'Select cameras'} />}
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {app.all_cameras ? (
          <div className="text-[var(--text-dim)]">
            Runs on <b className="text-[var(--text)]">every camera</b> — the platform selects them for this app.
          </div>
        ) : !takesPicks ? (
          <div className="text-[var(--text-dim)]">
            Works on other apps&apos; alerts, not on camera streams — no camera selection.
          </div>
        ) : picks.isPending ? (
          <div className="text-[var(--text-dim)]">Loading cameras…</div>
        ) : cameras.length === 0 ? (
          <div className="text-[var(--warn,var(--text-dim))]">
            No cameras selected — this app does nothing until one is.
          </div>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {cameras.map((c) => (
              <Badge key={c.id} variant={c.live_online === false ? 'warning' : 'neutral'}
                     title={c.live_online === false ? 'Stream down' : c.location ?? undefined}>
                {c.name}
              </Badge>
            ))}
          </div>
        )}
        {skills.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-[var(--text-dim)]">
            <Sparkles size={12} /> Brings to those cameras:
            {skills.map((s) => <Badge key={s} variant="neutral">{s.replace(/_/g, ' ')}</Badge>)}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
