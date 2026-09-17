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

// Which cameras this app works on — selected here, in the app's own settings.
//
// Every camera is available to every app, and any number of apps may use
// the same one. No cameras selected means the app does nothing and uses no
// compute, which is also where every freshly installed app starts.
//
// Two surfaces, because "what does this app watch?" and "which of my
// forty cameras should it watch?" are different questions:
//
//  * the section lists ONLY the app's cameras, as cards — picture, name,
//    location, whether it is online, and what is still to set up on it;
//  * "Select cameras" opens a dialog of thumbnails, searchable and grouped
//    by location with select-all per group, because people recognise a
//    doorway from its picture long before they recognise "Cam 14".
//
// Everything is a DRAFT, like every other field in the configuration form:
// nothing reaches the server until Save, and Cancel throws it away. On
// Save, `savePicks` writes each changed camera as a claim (consumer
// `app:<id>`), the same way the Vehicles page writes ANPR's gate cameras.
//
// The system handle (`cam3`) is never shown: it means nothing to the
// person choosing. Two cameras with the same name get `#id` to tell them
// apart, and only then.

import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Camera as CameraIcon, CameraOff, Check, Lock, Search, X } from 'lucide-react'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'
import { Button } from '../../components/ui'
import { StackedDialog } from './StackedDialog'

export type PickerCamera = {
  id: number
  handle: string
  name: string
  location?: string | null
  is_active: boolean
  /** Stream up (true), down (false), or paused / not known yet (null). */
  live_online?: boolean | null
  picked: boolean
  can_manage: boolean
  /** Names of the OTHER apps using this camera. */
  used_by?: string[]
}

export type PickerResponse = {
  app_id: string
  skill: string
  consumer: string
  camera_picker: boolean
  cameras: PickerCamera[]
}

/** Something still to do (or done) on one camera, e.g. "Scan zone". */
export type CameraSetupItem = { label: string; done: boolean }

/** The React Query key for an app's camera list. */
export const appCamerasKey = (appId: string) => ['app-cameras', appId]

export function useAppCameras(appId: string, enabled = true) {
  return useQuery<PickerResponse>({
    queryKey: appCamerasKey(appId),
    queryFn: async () => (await apiService.getAppCameras(appId)).data,
    staleTime: 10_000,
    enabled,
  })
}

/** The saved selection, as a set of camera ids. */
export function savedPicks(data: PickerResponse | undefined): Set<number> | null {
  if (!data) return null
  return new Set(data.cameras.filter((c) => c.picked).map((c) => c.id))
}

/** Write the difference between the saved selection and the draft.
 *  Returns whether anything changed. Stops at the first failure; the
 *  caller refetches, so the form then shows what actually got saved. */
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

/** The draft selection of the app whose configuration is being edited.
 *  Zone editors read it to offer only those cameras — including one added
 *  a moment ago and not saved yet, so a zone can be drawn in the same
 *  visit. ``null`` outside an app's configuration (no filtering). */
export const AppCameraScope = createContext<Set<number> | null>(null)

export function usePickedCameraIds(): Set<number> | null {
  return useContext(AppCameraScope)
}

/** A camera as people know it: its name, with `#id` only when another
 *  camera in the same list shares that name. */
export function cameraLabel(
  cam: { id: number | string; name?: string | null },
  all: { id: number | string; name?: string | null }[],
): string {
  if (!cam.name) return `Camera #${cam.id}`
  const clash = all.some((o) => o.id !== cam.id && o.name === cam.name)
  return clash ? `${cam.name} #${cam.id}` : cam.name
}

/** Names as people know them; `#id` only where two cameras share one. */
function useDisplayNames(cameras: PickerCamera[]) {
  return useMemo(() => {
    const count = new Map<string, number>()
    for (const c of cameras) count.set(c.name, (count.get(c.name) ?? 0) + 1)
    const out = new Map<number, string>()
    for (const c of cameras) {
      out.set(c.id, (count.get(c.name) ?? 0) > 1 ? `${c.name} #${c.id}` : c.name)
    }
    return out
  }, [cameras])
}

function rolesConfirmed(
  removing: PickerCamera[],
  names: Map<number, string>,
  cameraRoles?: Record<string, string>,
): boolean {
  const withRole = removing.filter((c) => cameraRoles?.[String(c.id)])
  if (withRole.length === 0) return true
  const list = withRole
    .map((c) => `${names.get(c.id)} ("${cameraRoles![String(c.id)]}")`)
    .join(', ')
  return window.confirm(
    `${list} ${withRole.length === 1 ? 'has a role' : 'have roles'}. Removing ${
      withRole.length === 1 ? 'it' : 'them'} stops that role from doing anything. Remove anyway?`,
  )
}

export function CameraPicker({
  appName,
  query,
  draft,
  onChange,
  cameraRoles,
  setup,
  onSetUp,
}: {
  appName: string
  query: ReturnType<typeof useAppCameras>
  draft: Set<number> | null
  onChange: (next: Set<number>) => void
  /** Per-camera roles the app keeps elsewhere (ANPR's gate roles, set on
   *  the Vehicles page) — shown on the card, because removing a camera
   *  silently stops its role from doing anything. */
  cameraRoles?: Record<string, string>
  /** What is set up on each camera, from this form's per-camera fields. */
  setup?: (cameraId: number) => CameraSetupItem[]
  /** Open this camera's setup (its per-camera shapes). */
  onSetUp?: (cameraId: number) => void
}) {
  const [choosing, setChoosing] = useState(false)
  const cameras = query.data?.cameras ?? []
  const names = useDisplayNames(cameras)

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

  const selected = cameras.filter((c) => draft.has(c.id))
  const unsaved = cameras.some((c) => draft.has(c.id) !== c.picked)

  const remove = (cam: PickerCamera) => {
    if (!rolesConfirmed([cam], names, cameraRoles)) return
    const next = new Set(draft)
    next.delete(cam.id)
    onChange(next)
  }

  const dialog = choosing && (
    <SelectCamerasDialog
      appName={appName}
      cameras={cameras}
      names={names}
      initial={draft}
      onClose={() => setChoosing(false)}
      onDone={(next) => {
        const removing = cameras.filter((c) => draft.has(c.id) && !next.has(c.id))
        if (!rolesConfirmed(removing, names, cameraRoles)) return
        onChange(next)
        setChoosing(false)
      }}
    />
  )

  if (selected.length === 0) {
    return (
      <>
        <div className="flex flex-col items-center gap-2 border border-dashed border-[var(--border)] px-4 py-6 text-center">
          <CameraOff size={22} className="text-[var(--text-dim)]" />
          <div className="text-sm font-medium">No cameras selected</div>
          <div className="max-w-sm text-xs text-[var(--text-dim)]">
            {appName} isn&apos;t running and uses no compute until you select at least one camera.
          </div>
          {cameras.length === 0 ? (
            <div className="text-xs text-[var(--text-dim)]">
              There are no cameras you can see. Add one on the Cameras page first.
            </div>
          ) : (
            <Button variant="primary" size="sm" onClick={() => setChoosing(true)}>
              Select cameras
            </Button>
          )}
          {unsaved && <UnsavedNote />}
        </div>
        {dialog}
      </>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <span className="text-xs text-[var(--text-dim)]">
          {selected.length} {selected.length === 1 ? 'camera' : 'cameras'}
        </span>
        <Button variant="outline" size="sm" className="ml-auto" onClick={() => setChoosing(true)}>
          Select cameras
        </Button>
      </div>
      <ul className="divide-y divide-[var(--border)] border border-[var(--border)]">
        {selected.map((cam) => {
          const role = cameraRoles?.[String(cam.id)]
          const items = setup?.(cam.id) ?? []
          return (
            <li key={cam.id} className="flex items-stretch gap-3 p-2">
              <CameraThumb cameraId={cam.id} className="h-[54px] w-[96px] shrink-0" />
              <div className="min-w-0 flex-1 space-y-0.5">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-[13px] font-medium" title={names.get(cam.id)}>
                    {names.get(cam.id)}
                  </span>
                  <LiveBadge cam={cam} />
                  {!cam.picked && (
                    <span className="shrink-0 text-[10px] text-[var(--accent)]">Not saved</span>
                  )}
                </div>
                {(cam.location || role) && (
                  <div className="truncate text-[11px] text-[var(--text-dim)]">
                    {cam.location}
                    {cam.location && role ? ' · ' : ''}
                    {role && <span title="Set on the Vehicles page">Role: {role}</span>}
                  </div>
                )}
                {items.length > 0 && (
                  <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px]">
                    {items.map((it) => (
                      <span
                        key={it.label}
                        className={it.done ? 'text-emerald-400' : 'text-[var(--text-dim)]'}
                      >
                        {it.done ? `✓ ${it.label}` : `${it.label}: not drawn`}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              {onSetUp && items.length > 0 && (
                <Button
                  variant={items.every((it) => it.done) ? 'ghost' : 'outline'}
                  size="sm"
                  className="self-center"
                  onClick={() => onSetUp(cam.id)}
                  title={cam.can_manage ? undefined : "You can't manage this camera — view only"}
                >
                  {!cam.can_manage ? 'View' : items.some((it) => it.done) ? 'Edit' : 'Set up'}
                </Button>
              )}
              <button
                type="button"
                onClick={() => remove(cam)}
                disabled={!cam.can_manage}
                title={cam.can_manage ? `Stop using ${names.get(cam.id)}` : "You can't manage this camera"}
                aria-label={`Remove ${names.get(cam.id)}`}
                className="self-center rounded p-1.5 text-[var(--text-dim)] hover:bg-[var(--panel)]
                           hover:text-[var(--text)] disabled:cursor-not-allowed disabled:opacity-40"
              >
                <X size={14} />
              </button>
            </li>
          )
        })}
      </ul>
      {unsaved
        ? <UnsavedNote />
        : (
          <p className="text-[11px] text-[var(--text-dim)]">
            Save applies changes; the running app picks them up within a few seconds.
          </p>
        )}
      {dialog}
    </div>
  )
}

function UnsavedNote() {
  return (
    <p className="text-[11px] text-[var(--accent)]">
      Not saved yet — Save applies it; the running app picks it up within a few seconds.
    </p>
  )
}

function LiveBadge({ cam }: { cam: PickerCamera }) {
  if (!cam.is_active) {
    return <span className="shrink-0 text-[10px] text-[var(--text-dim)]">Paused</span>
  }
  if (cam.live_online === true) {
    return <span className="shrink-0 text-[10px] text-emerald-400">● Online</span>
  }
  if (cam.live_online === false) {
    return <span className="shrink-0 text-[10px] text-amber-400">● Offline</span>
  }
  return null
}

// ── Thumbnails ──────────────────────────────────────────────────────

/** A camera still, fetched only once the tile scrolls into view — a site
 *  with a hundred cameras must not fire a hundred snapshots on open. Shares
 *  the zone editors' cache key, which holds the Blob; the URL is ours. */
function CameraThumb({ cameraId, className = '' }: { cameraId: number; className?: string }) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el || visible) return
    if (typeof IntersectionObserver === 'undefined') { setVisible(true); return }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { setVisible(true); io.disconnect() }
    }, { rootMargin: '100px' })
    io.observe(el)
    return () => io.disconnect()
  }, [visible])

  const query = useQuery({
    queryKey: ['camera-snapshot', cameraId],
    queryFn: async () => (await apiService.getCameraSnapshot(cameraId)).data as Blob,
    enabled: visible,
    retry: 0,
    staleTime: 30_000,
  })
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    const blob = query.data
    if (!blob) { setUrl(null); return }
    const next = URL.createObjectURL(blob)
    setUrl(next)
    return () => URL.revokeObjectURL(next)
  }, [query.data])

  return (
    <div ref={ref} className={`flex items-center justify-center overflow-hidden bg-black ${className}`}>
      {url
        ? <img src={url} alt="" className="h-full w-full object-cover" />
        : query.isError
          ? <CameraOff size={16} className="text-[var(--text-dim)]" />
          : <CameraIcon size={16} className="text-[var(--text-dim)] opacity-50" />}
    </div>
  )
}

// ── The selection dialog ────────────────────────────────────────────

const NO_LOCATION = 'No location'

function SelectCamerasDialog({
  appName, cameras, names, initial, onClose, onDone,
}: {
  appName: string
  cameras: PickerCamera[]
  names: Map<number, string>
  initial: Set<number>
  onClose: () => void
  onDone: (next: Set<number>) => void
}) {
  const [chosen, setChosen] = useState<Set<number>>(() => new Set(initial))
  const [search, setSearch] = useState('')
  const [location, setLocation] = useState('')
  const [onlineOnly, setOnlineOnly] = useState(false)

  const locations = useMemo(
    () => Array.from(new Set(cameras.map((c) => c.location?.trim() || NO_LOCATION)))
      .sort((a, b) => (a === NO_LOCATION ? 1 : b === NO_LOCATION ? -1 : a.localeCompare(b))),
    [cameras],
  )

  const groups = useMemo(() => {
    const q = search.trim().toLowerCase()
    const shown = cameras.filter((c) => {
      const loc = c.location?.trim() || NO_LOCATION
      if (location && loc !== location) return false
      if (onlineOnly && c.live_online !== true) return false
      if (!q) return true
      return (names.get(c.id) ?? c.name).toLowerCase().includes(q)
        || (c.location ?? '').toLowerCase().includes(q)
    })
    const byLoc = new Map<string, PickerCamera[]>()
    for (const c of shown) {
      const loc = c.location?.trim() || NO_LOCATION
      byLoc.set(loc, [...(byLoc.get(loc) ?? []), c])
    }
    return locations.filter((l) => byLoc.has(l)).map((l) => ({ location: l, cameras: byLoc.get(l)! }))
  }, [cameras, names, search, location, onlineOnly, locations])

  const toggle = (cam: PickerCamera) => {
    if (!cam.can_manage) return
    setChosen((prev) => {
      const next = new Set(prev)
      if (next.has(cam.id)) next.delete(cam.id)
      else next.add(cam.id)
      return next
    })
  }

  const toggleGroup = (group: PickerCamera[]) => {
    const manageable = group.filter((c) => c.can_manage)
    const allOn = manageable.every((c) => chosen.has(c.id))
    setChosen((prev) => {
      const next = new Set(prev)
      for (const c of manageable) {
        if (allOn) next.delete(c.id)
        else next.add(c.id)
      }
      return next
    })
  }

  const count = chosen.size

  return (
    <StackedDialog
      title={`Select cameras for ${appName}`}
      onClose={onClose}
      footer={(
        <>
          <span className="text-xs text-[var(--text-dim)]">
            {count === 0 ? 'No cameras selected' : `${count} selected`}
          </span>
          <Button variant="ghost" size="sm" className="ml-auto" onClick={onClose}>Cancel</Button>
          <Button variant="primary" size="sm" onClick={() => onDone(chosen)}>Done</Button>
        </>
      )}
    >

        <div className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] px-4 py-2">
          <label className="flex min-w-[220px] flex-1 items-center gap-2 border border-[var(--border)]
                            bg-[var(--bg-2)] px-2 py-1">
            <Search size={13} className="text-[var(--text-dim)]" />
            <input
              autoFocus
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search name or location"
              className="w-full bg-transparent text-xs outline-none"
            />
          </label>
          {locations.length > 1 && (
            <select
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              aria-label="Location"
              className="border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-xs"
            >
              <option value="">All locations</option>
              {locations.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
          )}
          <label className="flex items-center gap-1.5 text-xs text-[var(--text-dim)]">
            <input type="checkbox" checked={onlineOnly} onChange={(e) => setOnlineOnly(e.target.checked)} />
            Online only
          </label>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-3">
          {groups.length === 0 && (
            <p className="py-8 text-center text-xs text-[var(--text-dim)]">No cameras match.</p>
          )}
          {groups.map((g) => {
            const manageable = g.cameras.filter((c) => c.can_manage)
            const allOn = manageable.length > 0 && manageable.every((c) => chosen.has(c.id))
            return (
              <section key={g.location}>
                <div className="mb-2 flex items-center gap-2">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-[var(--text-dim)]">
                    {g.location}
                  </h3>
                  {manageable.length > 1 && (
                    <button type="button" onClick={() => toggleGroup(g.cameras)}
                            className="text-[11px] text-[var(--accent)] hover:underline">
                      {allOn ? 'Clear all' : `Select all (${manageable.length})`}
                    </button>
                  )}
                </div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4">
                  {g.cameras.map((cam) => {
                    const on = chosen.has(cam.id)
                    return (
                      <button
                        key={cam.id}
                        type="button"
                        onClick={() => toggle(cam)}
                        disabled={!cam.can_manage}
                        aria-pressed={on}
                        title={cam.can_manage ? undefined : "You can't manage this camera"}
                        className={`group relative flex flex-col overflow-hidden border text-left transition-colors
                          focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent)] ${
                          on ? 'border-[var(--accent)] ring-1 ring-[var(--accent)]'
                             : 'border-[var(--border)] hover:border-[var(--accent)]/60'
                        } ${cam.can_manage ? '' : 'cursor-not-allowed opacity-60'}`}
                      >
                        <CameraThumb cameraId={cam.id} className="aspect-video w-full" />
                        <span className={`absolute right-1.5 top-1.5 flex h-5 w-5 items-center justify-center border ${
                          on ? 'border-[var(--accent)] bg-[var(--accent)]' : 'border-white/60 bg-black/40'}`}>
                          {on && <Check size={13} className="text-white" />}
                          {!cam.can_manage && <Lock size={11} className="text-white" />}
                        </span>
                        <span className="space-y-0.5 px-2 py-1.5">
                          <span className="flex items-center gap-1.5">
                            <span className="truncate text-xs font-medium">{names.get(cam.id)}</span>
                            <LiveBadge cam={cam} />
                          </span>
                          {cam.used_by && cam.used_by.length > 0 && (
                            <span className="block truncate text-[10px] text-[var(--text-dim)]"
                                  title={`Also used by ${cam.used_by.join(', ')} — that's fine, apps share cameras.`}>
                              Also used by {cam.used_by.join(', ')}
                            </span>
                          )}
                          {!cam.can_manage && (
                            <span className="block text-[10px] text-[var(--text-dim)]">You can&apos;t manage this camera</span>
                          )}
                        </span>
                      </button>
                    )
                  })}
                </div>
              </section>
            )
          })}
        </div>

    </StackedDialog>
  )
}
