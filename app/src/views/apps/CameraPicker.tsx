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

// Which cameras this app works on — picked here, in the app's own settings.
//
// Every camera is available to every app, and any number of apps may pick
// the same one. Nothing picked means the app does nothing and uses no
// compute, which is also where every freshly installed app starts, so the
// empty state says so loudly rather than looking like a quiet success.
//
// Ticks are a DRAFT, like every other field in the configuration form:
// nothing reaches the server until Save, and Cancel throws them away. On
// Save, `savePicks` writes each changed camera as a claim (consumer
// `app:<id>`), the same way the Vehicles page writes ANPR's gate cameras,
// and the running app notices within one config poll.

import { createContext, useContext } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check } from 'lucide-react'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'

export type PickerCamera = {
  id: number
  handle: string
  name: string
  location?: string | null
  is_active: boolean
  picked: boolean
  can_manage: boolean
}

export type PickerResponse = {
  app_id: string
  skill: string
  consumer: string
  camera_picker: boolean
  cameras: PickerCamera[]
}

/** The React Query key for an app's picker. */
export const appCamerasKey = (appId: string) => ['app-cameras', appId]

export function useAppCameras(appId: string, enabled = true) {
  return useQuery<PickerResponse>({
    queryKey: appCamerasKey(appId),
    queryFn: async () => (await apiService.getAppCameras(appId)).data,
    staleTime: 10_000,
    enabled,
  })
}

/** The saved picks, as a set of camera ids. */
export function savedPicks(data: PickerResponse | undefined): Set<number> | null {
  if (!data) return null
  return new Set(data.cameras.filter((c) => c.picked).map((c) => c.id))
}

/** Write the difference between the saved picks and the draft. Returns
 *  whether anything changed. Stops at the first failure; the caller
 *  refetches, so the form then shows what actually got saved. */
export async function savePicks(data: PickerResponse, draft: Set<number>): Promise<boolean> {
  let changed = false
  for (const cam of data.cameras) {
    const want = draft.has(cam.id)
    if (want === cam.picked) continue
    if (want) await apiService.declareSkillCamera(data.skill, cam.id, data.consumer)
    else await apiService.releaseSkillCamera(data.skill, cam.id, data.consumer)
    changed = true
  }
  return changed
}

/** The draft picks of the app whose configuration is being edited. Zone
 *  editors read it to offer only those cameras — including one ticked a
 *  moment ago and not saved yet, so a zone can be drawn in the same visit.
 *  ``null`` outside an app's configuration (no filtering). */
export const AppCameraScope = createContext<Set<number> | null>(null)

export function usePickedCameraIds(): Set<number> | null {
  return useContext(AppCameraScope)
}

export function CameraPicker({
  query,
  draft,
  onChange,
  cameraRoles,
}: {
  query: ReturnType<typeof useAppCameras>
  draft: Set<number> | null
  onChange: (next: Set<number>) => void
  /** Per-camera roles the app keeps elsewhere (ANPR's gate roles, set on
   *  the Vehicles page) — shown read-only beside the camera, because
   *  unpicking a camera silently stops its role from doing anything. */
  cameraRoles?: Record<string, string>
}) {
  if (query.isLoading || (query.data && !draft)) {
    return <p className="text-xs text-[var(--text-dim)]">Loading cameras…</p>
  }
  if (query.isError || !query.data || !draft) {
    return (
      <p className="text-xs text-[var(--danger)]">
        {extractApiError(query.error, 'Could not load cameras.')}
      </p>
    )
  }

  const onToggle = (cam: PickerCamera) => {
    const next = new Set(draft)
    if (next.has(cam.id)) {
      const role = cameraRoles?.[String(cam.id)]
      if (role
          && !window.confirm(
            `${cam.name} has the role "${role}". Deselecting it stops that role from doing anything. Deselect anyway?`,
          )) {
        return
      }
      next.delete(cam.id)
    } else {
      next.add(cam.id)
    }
    onChange(next)
  }

  const cameras = query.data.cameras
  const picked = cameras.filter((c) => draft.has(c.id)).length
  const unsaved = cameras.some((c) => draft.has(c.id) !== c.picked)

  return (
    <div className="space-y-2">
      {picked === 0 && (
        <div
          role="status"
          className="border border-[var(--danger)]/50 bg-[var(--danger)]/10 px-3 py-2 text-xs text-[var(--danger)]"
        >
          No cameras selected — this app isn't running and uses no compute.
        </div>
      )}
      {cameras.length === 0 ? (
        <p className="text-xs text-[var(--text-dim)]">
          There are no cameras you can see. Add one on the Cameras page first.
        </p>
      ) : (
        <div className="max-h-64 overflow-y-auto border border-[var(--border)]">
          {cameras.map((cam) => {
            const on = draft.has(cam.id)
            const disabled = !cam.can_manage
            const role = cameraRoles?.[String(cam.id)]
            return (
              <button
                key={cam.id}
                type="button"
                onClick={() => !disabled && onToggle(cam)}
                disabled={disabled}
                aria-pressed={on}
                title={cam.can_manage ? undefined : "You can't manage this camera"}
                className={`group flex w-full items-center gap-2 border-l-2 px-2.5 py-1.5 text-left text-[13px] transition-colors focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent)] ${
                  on ? 'border-l-[var(--accent)] bg-[var(--accent)]/10' : 'border-l-transparent'
                } ${!cam.can_manage ? 'cursor-not-allowed opacity-50' : 'hover:bg-[var(--panel)]'}`}
              >
                <span
                  className={`flex h-4 w-4 shrink-0 items-center justify-center border transition-colors ${
                    on
                      ? 'border-[var(--accent)] bg-[var(--accent)]'
                      : 'border-[var(--border)] group-hover:border-[var(--accent)]/60'
                  }`}
                >
                  {on && <Check size={12} className="text-white" />}
                </span>
                <span className="min-w-0 flex-1 truncate">
                  {cam.name}
                  {cam.location && <span className="text-[var(--text-dim)]"> · {cam.location}</span>}
                </span>
                {role && (
                  <span className="shrink-0 text-[10px] text-[var(--text-dim)]" title="Set on the Vehicles page">
                    {role}
                  </span>
                )}
                {!cam.is_active && (
                  <span className="shrink-0 text-[10px] text-[var(--text-dim)]">paused</span>
                )}
                <span className="shrink-0 font-mono text-[10px] text-[var(--text-dim)]">{cam.handle}</span>
              </button>
            )
          })}
        </div>
      )}
      <p className="text-[11px] text-[var(--text-dim)]">
        {picked > 0 ? `${picked} selected. ` : ''}
        {unsaved
          ? 'Not saved yet — Save applies it; the running app picks it up within a few seconds.'
          : 'Save applies changes; the running app picks them up within a few seconds.'}
      </p>
    </div>
  )
}
