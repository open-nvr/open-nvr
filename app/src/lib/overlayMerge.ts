// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// One camera's overlay is fed by several sources: the platform detector
// ("tier0") and every app the operator lets draw ("app:<id>"). Each
// source's frames describe only that source's boxes, so a frame from one
// must never retire another's — and an app box that names the Tier-0
// track it labels (overlay.boxes.v1 `track`) must not draw a second box
// over the same person: its label goes onto the track's own box, which
// moves at the detector's rate instead of the app's.
//
// Pure, so tests run it without a canvas.

export const TIER0_SOURCE = 'tier0'

export interface MergeTrack {
  id: number | string | null
  label: string
  score: number | null
  track?: number | string | null
}

export interface MergeEntry {
  source: string
  track: MergeTrack
  /** Set once the entry's source stopped carrying it (fading out). */
  lostAt: number | null
}

export interface MergePlan<E extends MergeEntry> {
  /** Tier-0 entry key → the app entry whose label it now wears. */
  labelled: Map<string, E>
  /** App entry keys not drawn: their label is on a Tier-0 box. */
  hidden: Set<string>
}

const idKey = (v: number | string | null | undefined) => (v == null ? null : String(v))

/** Which app labels go on which Tier-0 boxes. A claim counts while the
 *  app entry is live; the first app to claim a track keeps it, so two
 *  apps never fight over one label slot frame by frame. */
export function planMerge<E extends MergeEntry>(entries: Map<string, E>): MergePlan<E> {
  const tier0ById = new Map<string, string>()   // tier0 track id → entry key
  for (const [key, e] of entries) {
    if (e.source !== TIER0_SOURCE) continue
    const id = idKey(e.track.id)
    if (id != null && !tier0ById.has(id)) tier0ById.set(id, key)
  }
  const labelled = new Map<string, E>()
  const hidden = new Set<string>()
  for (const [key, e] of entries) {
    if (e.source === TIER0_SOURCE || e.lostAt != null) continue
    const ref = idKey(e.track.track)
    if (ref == null) continue
    const target = tier0ById.get(ref)
    if (target == null) continue      // track not on screen: the app box draws itself
    if (!labelled.has(target)) labelled.set(target, e)
    hidden.add(key)
  }
  return { labelled, hidden }
}

/** The track a labelled Tier-0 box is drawn as: the app's words, none of
 *  the detector's "#12 87%" (the label is a statement about who, not a
 *  guess about what). */
export function wearLabel<T extends MergeTrack>(base: T, app: MergeTrack): T {
  return { ...base, label: app.label, id: null, score: null }
}
