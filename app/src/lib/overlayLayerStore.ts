// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What each camera's live overlay draws, and what the viewer switched off
// in each VIEW of it.
//
// The layers a camera draws are the camera's: every DetectionOverlay
// reports them here, and every view lists the same ones. What is switched
// off is the view's: Live View, the dashboard wall, tours, the peek
// dialog and each app's page keep their own choice per camera (a
// "scope"), so an app page can show only its app's boxes while Live View
// shows only the detector's people. Each choice is remembered in this
// browser, per camera and view, and reaches views of the same scope that
// are already open, and other tabs (storage event).

import { useMemo, useSyncExternalStore } from 'react'
import { hiddenLayersKey, type OverlayLayer } from './overlayLayers'

/** A layer stays in the list this long after it was last drawn, so the
 *  list does not flicker as boxes come and go between frames. */
const KEEP_MS = 30_000

/** Where a camera is shown: 'live', 'dashboard', 'tour', 'peek',
 *  'app:<app id>'. Each keeps its own choice. */
export type LayerScope = string
export const DEFAULT_SCOPE: LayerScope = 'live'

type Entry = { layer: OverlayLayer; seen: number }
type Snapshot = { layers: OverlayLayer[]; hidden: ReadonlySet<string> }

const seenBy = new Map<number, Map<string, Entry>>()      // per camera
const hiddenBy = new Map<string, Set<string>>()           // per camera + scope
const listeners = new Map<number, Set<() => void>>()      // per camera
const snapshots = new Map<string, Snapshot>()             // per camera + scope

const hk = (cameraId: number, scope: LayerScope) => `${cameraId}|${scope}`

function readHidden(cameraId: number, scope: LayerScope): Set<string> {
  try {
    const v = JSON.parse(window.localStorage.getItem(hiddenLayersKey(cameraId, scope)) || '[]')
    return new Set(Array.isArray(v) ? v.filter((k): k is string => typeof k === 'string') : [])
  } catch {
    return new Set()
  }
}

function hiddenOf(cameraId: number, scope: LayerScope): Set<string> {
  const k = hk(cameraId, scope)
  let h = hiddenBy.get(k)
  if (!h) {
    h = readHidden(cameraId, scope)
    hiddenBy.set(k, h)
  }
  return h
}

/** Something about this camera changed: every view of it re-reads. */
function emit(cameraId: number): void {
  for (const k of [...snapshots.keys()]) if (k.startsWith(`${cameraId}|`)) snapshots.delete(k)
  for (const fn of listeners.get(cameraId) ?? []) fn()
}

function persist(cameraId: number, scope: LayerScope): void {
  const h = hiddenOf(cameraId, scope)
  try {
    if (h.size) window.localStorage.setItem(hiddenLayersKey(cameraId, scope), JSON.stringify([...h]))
    else window.localStorage.removeItem(hiddenLayersKey(cameraId, scope))
  } catch {
    // Not remembering a choice is not an error.
  }
  emit(cameraId)
}

/** What the viewer switched off on this camera in this view. Read by the
 *  draw loop every frame. */
export function hiddenLayers(cameraId: number, scope: LayerScope = DEFAULT_SCOPE): ReadonlySet<string> {
  return hiddenOf(cameraId, scope)
}

/** An overlay reports what it is drawing now. Cheap to call often: it only
 *  notifies when the list of layers actually changes. */
export function reportLayers(cameraId: number, layers: OverlayLayer[]): void {
  const now = Date.now()
  let seen = seenBy.get(cameraId)
  if (!seen) {
    seen = new Map()
    seenBy.set(cameraId, seen)
  }
  let changed = false
  for (const l of layers) {
    const had = seen.get(l.key)
    if (!had || had.layer.name !== l.name) changed = true
    seen.set(l.key, { layer: l, seen: now })
  }
  for (const [k, e] of seen) {
    if (now - e.seen > KEEP_MS) {
      seen.delete(k)
      changed = true
    }
  }
  if (changed) emit(cameraId)
}

export function setLayerHidden(cameraId: number, scope: LayerScope, key: string, hidden: boolean): void {
  setLayersHidden(cameraId, scope, [key], hidden)
}

/** Hide or show several layers at once: a group's all/none, or every one. */
export function setLayersHidden(cameraId: number, scope: LayerScope, keys: Iterable<string>, hidden: boolean): void {
  const h = hiddenOf(cameraId, scope)
  for (const k of keys) {
    if (hidden) h.add(k)
    else h.delete(k)
  }
  persist(cameraId, scope)
}

/** Everything back on in this view, including layers not drawn right now. */
export function showAllLayers(cameraId: number, scope: LayerScope): void {
  hiddenOf(cameraId, scope).clear()
  persist(cameraId, scope)
}

function snapshot(cameraId: number, scope: LayerScope): Snapshot {
  const k = hk(cameraId, scope)
  let s = snapshots.get(k)
  if (!s) {
    const layers = [...(seenBy.get(cameraId)?.values() ?? [])].map((e) => e.layer)
      .sort((a, b) => a.group.localeCompare(b.group) || a.name.localeCompare(b.name))
    s = { layers, hidden: new Set(hiddenOf(cameraId, scope)) }
    snapshots.set(k, s)
  }
  return s
}

const EMPTY: Snapshot = { layers: [], hidden: new Set() }

function subscribe(cameraId: number | null | undefined) {
  return (fn: () => void) => {
    if (cameraId == null) return () => {}
    let set = listeners.get(cameraId)
    if (!set) {
      set = new Set()
      listeners.set(cameraId, set)
    }
    set.add(fn)
    return () => { set?.delete(fn) }
  }
}

/** The layers this camera draws, and the ones switched off in this view. */
export function useOverlayLayers(cameraId: number | null | undefined, scope: LayerScope = DEFAULT_SCOPE): Snapshot {
  // One subscribe function per camera: a new one each render would make
  // React unsubscribe and subscribe again on every render.
  const sub = useMemo(() => subscribe(cameraId), [cameraId])
  return useSyncExternalStore(sub, () => (cameraId == null ? EMPTY : snapshot(cameraId, scope)))
}

/** Subscribe outside React (the overlay's draw loop redraws on a change). */
export function onLayersChange(cameraId: number, fn: () => void): () => void {
  return subscribe(cameraId)(fn)
}

// Another tab changed a choice: pick it up here too.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e) => {
    const m = e.key ? /^opennvr\.overlay\.hidden\.(\d+)\.(.+)$/.exec(e.key) : null
    if (!m) return
    const id = Number(m[1])
    hiddenBy.set(hk(id, m[2]), readHidden(id, m[2]))
    emit(id)
  })
}
