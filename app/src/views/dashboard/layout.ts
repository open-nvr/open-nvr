// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The dashboard's widget grid: which widgets exist, where they sit by
 * default, and the per-user, per-browser persistence of the operator's own
 * arrangement.
 *
 * The grid is GRID_COLS wide and GRID_ROWS tall, and the dashboard sizes a
 * row so that GRID_ROWS rows exactly fill the viewport below the health bar.
 * The default layout uses every row and no more, which is what makes the
 * whole dashboard visible without scrolling on a desktop screen. An operator
 * who adds or enlarges widgets past that simply gets a page that scrolls.
 */

import { useCallback, useEffect, useState } from 'react'

export const GRID_COLS = 12
export const GRID_ROWS = 24

export type WidgetId =
  | 'vitals'
  | 'live'
  | 'alarms'
  | 'cameras'
  | 'resources'
  | 'footage'
  | 'ids'

export type WidgetPlacement = { i: WidgetId; x: number; y: number; w: number; h: number }

/** Size limits per widget, in grid units. A widget smaller than its minimum
 *  cannot show its content without clipping, so the resize handle stops there. */
export const WIDGET_LIMITS: Record<WidgetId, { minW: number; minH: number }> = {
  vitals: { minW: 4, minH: 3 },
  live: { minW: 3, minH: 5 },
  alarms: { minW: 3, minH: 4 },
  cameras: { minW: 3, minH: 4 },
  resources: { minW: 3, minH: 4 },
  footage: { minW: 3, minH: 4 },
  ids: { minW: 3, minH: 4 },
}

/** Every widget, in the order the "Add widget" menu lists them. */
export const ALL_WIDGETS: WidgetId[] = ['vitals', 'live', 'alarms', 'cameras', 'resources', 'footage', 'ids']

export const DEFAULT_LAYOUT: WidgetPlacement[] = [
  { i: 'vitals', x: 0, y: 0, w: 12, h: 4 },
  { i: 'live', x: 0, y: 4, w: 8, h: 12 },
  { i: 'alarms', x: 8, y: 4, w: 4, h: 12 },
  { i: 'resources', x: 0, y: 16, w: 4, h: 8 },
  { i: 'cameras', x: 4, y: 16, w: 4, h: 8 },
  { i: 'footage', x: 8, y: 16, w: 4, h: 8 },
]

/** Where a widget lands when added back from the menu: full width, below
 *  everything. The grid's vertical compaction then pulls it up into any gap. */
const ADDED_SIZE: Record<WidgetId, { w: number; h: number }> = {
  vitals: { w: 12, h: 4 },
  live: { w: 8, h: 12 },
  alarms: { w: 4, h: 10 },
  cameras: { w: 4, h: 8 },
  resources: { w: 4, h: 8 },
  footage: { w: 4, h: 8 },
  ids: { w: 4, h: 8 },
}

// Bump when the widget set or grid geometry changes incompatibly; a stored
// layout under an older version is discarded rather than half-applied.
const STORAGE_VERSION = 1

function storageKey(userId: number | string | null | undefined) {
  return `opennvr.dashboard.layout.v${STORAGE_VERSION}.${userId ?? 'anon'}`
}

function isPlacement(v: unknown): v is WidgetPlacement {
  if (!v || typeof v !== 'object') return false
  const p = v as Record<string, unknown>
  return (
    typeof p.i === 'string' && (ALL_WIDGETS as string[]).includes(p.i) &&
    [p.x, p.y, p.w, p.h].every((n) => typeof n === 'number' && Number.isFinite(n) && n >= 0)
  )
}

function load(userId: number | string | null | undefined): WidgetPlacement[] | null {
  try {
    const raw = localStorage.getItem(storageKey(userId))
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return null
    const seen = new Set<string>()
    const out: WidgetPlacement[] = []
    for (const p of parsed) {
      if (!isPlacement(p) || seen.has(p.i)) continue
      seen.add(p.i)
      const lim = WIDGET_LIMITS[p.i]
      out.push({
        i: p.i,
        x: Math.min(p.x, GRID_COLS - 1),
        y: p.y,
        w: Math.max(lim.minW, Math.min(p.w, GRID_COLS)),
        h: Math.max(lim.minH, p.h),
      })
    }
    return out
  } catch {
    // Storage blocked or corrupt: fall back to the default arrangement.
    return null
  }
}

function save(userId: number | string | null | undefined, layout: WidgetPlacement[]) {
  try {
    localStorage.setItem(storageKey(userId), JSON.stringify(layout))
  } catch {
    // Private mode / quota: the arrangement just won't survive a reload.
  }
}

function clear(userId: number | string | null | undefined) {
  try {
    localStorage.removeItem(storageKey(userId))
  } catch {
    // ignore
  }
}

/**
 * The operator's layout, kept in localStorage under their user id so two
 * people sharing a control-room browser each keep their own arrangement.
 */
export function useDashboardLayout(userId: number | string | null | undefined) {
  const [layout, setLayoutState] = useState<WidgetPlacement[]>(() => load(userId) ?? DEFAULT_LAYOUT)
  const [customised, setCustomised] = useState(() => load(userId) != null)

  // The user can resolve after first render (auth refresh); pick up theirs.
  useEffect(() => {
    const stored = load(userId)
    setLayoutState(stored ?? DEFAULT_LAYOUT)
    setCustomised(stored != null)
  }, [userId])

  const setLayout = useCallback((next: WidgetPlacement[]) => {
    setLayoutState(next)
    setCustomised(true)
    save(userId, next)
  }, [userId])

  const remove = useCallback((id: WidgetId) => {
    setLayoutState((cur) => {
      const next = cur.filter((p) => p.i !== id)
      save(userId, next)
      return next
    })
    setCustomised(true)
  }, [userId])

  const add = useCallback((id: WidgetId) => {
    setLayoutState((cur) => {
      if (cur.some((p) => p.i === id)) return cur
      const bottom = cur.reduce((m, p) => Math.max(m, p.y + p.h), 0)
      const size = ADDED_SIZE[id]
      const next = [...cur, { i: id, x: 0, y: bottom, ...size }]
      save(userId, next)
      return next
    })
    setCustomised(true)
  }, [userId])

  const reset = useCallback(() => {
    clear(userId)
    setLayoutState(DEFAULT_LAYOUT)
    setCustomised(false)
  }, [userId])

  return { layout, setLayout, add, remove, reset, customised }
}

/**
 * Swap semantics for a drop. The grid's own behaviour when one widget is
 * dropped on another is to push the other one down, which on a screen-sized
 * layout shoves it below the fold and leaves a hole where the dragged widget
 * came from. Operators expect the two to trade places, so when the centre of
 * the drop lands on another widget's ORIGINAL slot, the two exchange slots
 * (position and size). Returns null when the drop is on empty space, or when
 * either widget would be smaller than its minimum in the other's slot — the
 * grid's ordinary move applies then.
 */
export function swapOnDrop(
  before: WidgetPlacement[],
  draggedId: WidgetId,
  drop: { x: number; y: number; w: number; h: number },
): WidgetPlacement[] | null {
  const src = before.find((p) => p.i === draggedId)
  if (!src) return null
  const cx = drop.x + drop.w / 2
  const cy = drop.y + drop.h / 2
  const target = before.find(
    (p) => p.i !== draggedId && cx >= p.x && cx < p.x + p.w && cy >= p.y && cy < p.y + p.h,
  )
  if (!target) return null
  const fits = (id: WidgetId, slot: WidgetPlacement) =>
    slot.w >= WIDGET_LIMITS[id].minW && slot.h >= WIDGET_LIMITS[id].minH
  if (!fits(src.i, target) || !fits(target.i, src)) return null
  return before.map((p) =>
    p.i === src.i ? { ...target, i: src.i }
      : p.i === target.i ? { ...src, i: target.i }
        : p,
  )
}
