// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The schedule of a Live View tour, as pure functions so it can be tested
 * without a browser (app/tests/tourPlan.test.mjs).
 *
 * A tour is pages; a page is one screenful: a layout, the camera on each of
 * its tiles, shown for `duration_s`. What the editor shows as a page is
 * exactly what the wall shows. A page's list is in tile order and may hold
 * empty tiles (null): taking a camera out leaves a gap, it never moves the
 * others. Pages with no camera the viewer may see are skipped, and a page
 * whose layout no longer exists falls back to an automatic grid.
 */

import type { LayoutDefinition, LayoutTile } from './liveLayouts'

/** A tile's camera, or null for a tile left empty. */
export type Slot = number | null

/** The cameras of a page's tile list, empty tiles left out. */
export const camerasOf = (ids: readonly Slot[]): number[] => ids.filter((c): c is number => c != null)

/** A page as GET /api/v1/tours/{id} returns it, already filtered to the viewer. */
export interface TourPageSpec {
  label: string
  layout_id: string
  duration_s: number
  /** In tile order; null is an empty tile. Never more than the layout's tiles. */
  camera_ids: Slot[]
  paused_camera_ids?: number[]
  hidden_count?: number
  layout_missing?: boolean
}

/** A page ready to show: its layout resolved. */
export interface TourPage {
  /** Stable across refetches of an unchanged tour, so the timer is not reset. */
  key: string
  /** Its position in the tour's pages, empty ones included. */
  index: number
  label: string
  layout: LayoutDefinition
  /** True when the page's layout is gone and an automatic grid stands in. */
  layoutFallback: boolean
  /** One per tile, in tile order: null (or missing, past the end) is empty. */
  cameraIds: Slot[]
  durationMs: number
}

/** The automatic grid never grows past 5×5: beyond that tiles are too small to watch. */
export const AUTO_GRID_MAX = 25

/** A near-square grid for n cameras (1..AUTO_GRID_MAX), widest first. */
export function autoGrid(n: number): LayoutDefinition {
  const count = Math.min(Math.max(1, Math.floor(n) || 1), AUTO_GRID_MAX)
  const cols = Math.ceil(Math.sqrt(count))
  const rows = Math.ceil(count / cols)
  const tiles: LayoutTile[] = []
  for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) tiles.push({ row: r, col: c, rowSpan: 1, colSpan: 1 })
  return { name: `${cols}×${rows}`, gridCols: cols, gridRows: rows, tiles }
}

/**
 * The pages of the tour in play order. `resolve` maps a layout id to its
 * definition, or null when Live View no longer offers that layout. A page
 * of empty tiles only is left out of a running tour; the editor keeps it
 * (`keepEmptyPages`) so the wall does not jump away from the page being
 * arranged and cameras can still be put on it.
 */
export function buildPages(
  pages: readonly TourPageSpec[],
  resolve: (layoutId: string) => LayoutDefinition | null,
  { keepEmptyPages = false }: { keepEmptyPages?: boolean } = {},
): TourPage[] {
  const out: TourPage[] = []
  pages.forEach((spec, index) => {
    const ids = spec.camera_ids ?? []
    if (!keepEmptyPages && camerasOf(ids).length === 0) return
    const resolved = spec.layout_missing ? null : resolve(spec.layout_id)
    const layout = resolved && resolved.tiles.length > 0 ? resolved : autoGrid(ids.length)
    out.push({
      key: `${index}:${spec.layout_id}:${ids.join(',')}`,
      index,
      label: spec.label,
      // Always the layout as chosen, tiles without a camera left empty: the
      // editor's preview draws these same pages, so what the admin arranges
      // is exactly what the wall shows. The server never sends more cameras
      // than tiles; a layout changed since is cut, not overflowed.
      layout,
      layoutFallback: !resolved,
      cameraIds: ids.slice(0, layout.tiles.length),
      durationMs: Math.max(1, spec.duration_s) * 1000,
    })
  })
  return out
}

/** Consecutive pages under one label, summed up for a list: "North ×3 · 54". */
export interface PageGroup {
  /** Index of its first page in the tour. */
  first: number
  count: number
  label: string
  /** Distinct cameras across its pages. */
  cameras: number
  layoutMissing: boolean
}

/** The tour's pages, consecutive ones with the same label as one group.
 *  Unlabelled pages always stand alone. */
export function pageGroups(pages: readonly TourPageSpec[]): PageGroup[] {
  const out: (PageGroup & { ids: Set<number> })[] = []
  pages.forEach((p, i) => {
    const last = out[out.length - 1]
    if (last && p.label && last.label === p.label) {
      last.count++
      camerasOf(p.camera_ids).forEach((c) => last.ids.add(c))
      last.layoutMissing ||= !!p.layout_missing
    } else {
      out.push({ first: i, count: 1, label: p.label, cameras: 0, layoutMissing: !!p.layout_missing, ids: new Set(camerasOf(p.camera_ids)) })
    }
  })
  return out.map(({ ids, ...g }) => ({ ...g, cameras: ids.size }))
}

/** Index `i` moved by `delta`, wrapping around a list of `n` (0 when empty). */
export function wrapIndex(i: number, delta: number, n: number): number {
  if (n <= 0) return 0
  return (((i + delta) % n) + n) % n
}

/** How long one full cycle of the tour takes, in seconds. */
export function cycleSeconds(pages: readonly TourPage[]): number {
  return pages.reduce((sum, p) => sum + p.durationMs / 1000, 0)
}

/** "45 s", "2 min", "1 h 5 min": a duration the way a person says it. */
export function formatDuration(seconds: number): string {
  const s = Math.round(seconds)
  if (s < 60) return `${s} s`
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const rest = s % 60
  if (h > 0) return m ? `${h} h ${m} min` : `${h} h`
  return rest ? `${m} min ${rest} s` : `${m} min`
}
