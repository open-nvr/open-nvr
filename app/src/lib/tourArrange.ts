// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The edits the tour editor makes to one
 * page's camera list, as pure functions (app/tests/tourArrange.test.mjs).
 *
 * A page's cameras are one list in tile order: position `i` is tile `i`.
 * An empty tile is null, and the positions past the end are the page's
 * empty tiles. A page holds at most as many cameras as its layout has
 * tiles (`tiles` below). Taking a camera out leaves its tile empty, so no
 * other camera moves on the wall. Every function returns a new list and
 * never puts a camera on a page twice; one camera may be on several pages.
 */

type Slot = number | null

/** Without the empty tiles at the end: they are simply where the page stops. */
export function tidy(ids: readonly Slot[]): Slot[] {
  let end = ids.length
  while (end > 0 && ids[end - 1] == null) end--
  return ids.slice(0, end)
}

/** Room up to position `at`: tiles past the end are empty. */
function padTo(ids: readonly Slot[], at: number): Slot[] {
  const out = [...ids]
  while (out.length <= at) out.push(null)
  return out
}

/** The camera at `at` taken out: its tile is left empty, no other moves. */
export function removeAt(ids: readonly Slot[], at: number): Slot[] {
  if (at < 0 || at >= ids.length) return [...ids]
  const out = [...ids]
  out[at] = null
  return tidy(out)
}

/**
 * The camera at `from` dropped on tile `to`: swapped with the camera
 * there, or, on an empty tile, moved there and its old tile left empty.
 */
export function moveTo(ids: readonly Slot[], from: number, to: number, tiles: number): Slot[] {
  if (from < 0 || from >= ids.length || ids[from] == null || from === to || to < 0 || to >= tiles) return [...ids]
  const out = padTo(ids, to)
  ;[out[from], out[to]] = [out[to], out[from]]
  return tidy(out)
}

/**
 * Camera `cam` put on tile `at`: it replaces the camera there or fills
 * that empty tile. A camera already on the page is moved instead (swapped
 * into place), so it never shows twice.
 */
export function placeAt(ids: readonly Slot[], cam: number, at: number, tiles: number): Slot[] {
  const existing = ids.indexOf(cam)
  if (existing >= 0) return moveTo(ids, existing, at, tiles)
  if (at < 0 || at >= tiles) return [...ids]
  const out = padTo(ids, at)
  out[at] = cam
  return tidy(out)
}

/** The page's first empty tile, or null when every tile has a camera. */
export function firstEmpty(ids: readonly Slot[], tiles: number): number | null {
  const gap = ids.indexOf(null)
  if (gap >= 0 && gap < tiles) return gap
  return ids.length < tiles ? ids.length : null
}

/** Camera `cam` added to the page: onto its first empty tile; a full page is unchanged. */
export function addCamera(ids: readonly Slot[], cam: number, tiles: number): Slot[] {
  if (ids.includes(cam)) return [...ids]
  const at = firstEmpty(ids, tiles)
  return at == null ? [...ids] : placeAt(ids, cam, at, tiles)
}

/**
 * `cams` added to the page in order, onto its empty tiles, and those that
 * did not fit (the rest goes on new pages, see `chunk`). Cameras already on
 * the page are skipped.
 */
export function fill(ids: readonly Slot[], cams: readonly number[], tiles: number): { ids: Slot[]; rest: number[] } {
  let out: Slot[] = [...ids]
  const rest: number[] = []
  for (const cam of cams) {
    if (out.includes(cam) || rest.includes(cam)) continue
    if (firstEmpty(out, tiles) == null) rest.push(cam)
    else out = addCamera(out, cam, tiles)
  }
  return { ids: out, rest }
}

/** `cams` laid out as pages of `tiles` cameras each. */
export function chunk(cams: readonly number[], tiles: number): Slot[][] {
  const per = Math.max(1, tiles)
  const out: Slot[][] = []
  for (let i = 0; i < cams.length; i += per) out.push(cams.slice(i, i + per))
  return out
}

/**
 * The page cut to a layout of `tiles`: the cameras on its first `tiles`
 * tiles stay where they are, and those beyond, in order, are returned to
 * go on new pages.
 */
export function fit(ids: readonly Slot[], tiles: number): { ids: Slot[]; rest: number[] } {
  return { ids: tidy(ids.slice(0, tiles)), rest: ids.slice(tiles).filter((c): c is number => c != null) }
}

/** The cameras packed together, every gap closed, in their order. */
export function closeGaps(ids: readonly Slot[]): Slot[] {
  return ids.filter((c) => c != null)
}

/**
 * What `placeAt(ids, cam, at, tiles)` will do, so the screen can say so:
 * above all when the camera is already on the page and moves instead of
 * being added a second time.
 */
export type PlaceOutcome =
  | { kind: 'added'; at: number }
  | { kind: 'replaced'; at: number; replaced: number }
  | { kind: 'moved'; from: number; at: number }
  | { kind: 'swapped'; from: number; at: number; other: number }
  | { kind: 'unchanged'; at: number }
  | { kind: 'full' }

export function placeOutcome(ids: readonly Slot[], cam: number, at: number, tiles: number): PlaceOutcome {
  const from = ids.indexOf(cam)
  if (from >= 0 && from === at) return { kind: 'unchanged', at }
  if (at < 0 || at >= tiles) return from >= 0 ? { kind: 'unchanged', at: from } : { kind: 'full' }
  const there = ids[at] ?? null
  if (from >= 0) return there == null ? { kind: 'moved', from, at } : { kind: 'swapped', from, at, other: there }
  return there == null ? { kind: 'added', at } : { kind: 'replaced', at, replaced: there }
}

/** The app's t(): a key and its values, in the operator's language. */
type Translate = (key: string, values?: Record<string, string | number>) => string

/**
 * What putting `cam` on tile `at` did, in words, or null when there is
 * nothing to say (a plain add). Above all, a camera already on the page
 * moves instead of showing twice, and the operator is told so.
 */
export function placeMessage(t: Translate, ids: readonly Slot[], cam: number, at: number, nameOf: (id: number) => string, tiles: number): string | null {
  const o = placeOutcome(ids, cam, at, tiles)
  const name = nameOf(cam)
  switch (o.kind) {
    case 'unchanged': return t('tours.placeSame', { name, n: o.at + 1 })
    case 'moved': return t('tours.placeMoved', { name, from: o.from + 1, to: o.at + 1 })
    case 'swapped': return t('tours.placeSwapped', { name, from: o.from + 1, to: o.at + 1, other: nameOf(o.other) })
    case 'replaced': return t('tours.placeReplaced', { name, other: nameOf(o.replaced), n: o.at + 1 })
    case 'full': return t('tours.placeFull', { max: tiles })
    default: return null
  }
}
