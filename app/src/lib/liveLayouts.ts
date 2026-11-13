// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The built-in Live View layouts, shared by the Live View grid and the
 * Window Settings page that enables them. One table, so the two cannot drift:
 * they were once separate copies, and both carried a "1+12" that had 8 tiles
 * and a "1+1+10" that had 10.
 *
 * A layout's name is its tile count: every "a+b" name must add up to
 * tiles.length, and the tiles must cover the grid exactly once.
 */

export interface LayoutTile {
  row: number
  col: number
  rowSpan: number
  colSpan: number
}

export interface LayoutDefinition {
  name: string
  description?: string
  gridCols: number
  gridRows: number
  tiles: LayoutTile[]
}

/** GET /api/v1/general/window-settings, the parts a layout lookup needs. */
export interface WindowLayoutSettings {
  layouts_enabled?: Record<string, boolean>
  custom_layouts?: Array<{
    id: string
    name: string
    enabled?: boolean
    grid_columns: number
    grid_rows: number
    tiles?: Array<Partial<LayoutTile>>
  }>
}

/**
 * A tour step may name a plain grid, "grid:<cols>x<rows>" (1..8 each), that
 * needs no Window Settings entry — the 20-camera "grid:5x4" wall without
 * first building a 20-tile custom layout by hand. [cols, rows] or null.
 */
export function parseGridId(id: string): [number, number] | null {
  const m = /^grid:([1-8])x([1-8])$/.exec(id)
  return m ? [Number(m[1]), Number(m[2])] : null
}

/** The plain grids a tour editor offers beyond the built-in layouts. */
export const PLAIN_GRIDS: Array<[number, number]> = [[4, 3], [5, 4], [6, 4], [6, 5], [8, 5], [8, 6]]

/**
 * The layout Live View would draw for `id` right now — an enabled custom
 * layout first, then an enabled built-in one — or null when it offers no
 * such layout (deleted or disabled). Custom tiles are not validated by the
 * server, so any that fall outside their grid are dropped.
 */
export function resolveLayout(id: string, settings: WindowLayoutSettings | null | undefined): LayoutDefinition | null {
  const plain = parseGridId(id)
  if (plain) return { name: `${plain[0]}×${plain[1]}`, gridCols: plain[0], gridRows: plain[1], tiles: cells(plain[0], plain[1]) }
  const custom = settings?.custom_layouts?.find((l) => l.id === id && l.enabled !== false)
  if (custom) {
    const cols = custom.grid_columns
    const rows = custom.grid_rows
    const tiles = (custom.tiles ?? [])
      .map((t) => ({ row: t.row ?? 0, col: t.col ?? 0, rowSpan: t.rowSpan ?? 1, colSpan: t.colSpan ?? 1 }))
      .filter((t) => t.row >= 0 && t.col >= 0 && t.rowSpan >= 1 && t.colSpan >= 1 && t.row + t.rowSpan <= rows && t.col + t.colSpan <= cols)
    return { name: custom.name, gridCols: cols, gridRows: rows, tiles: tiles.length ? tiles : cells(cols, rows) }
  }
  const builtIn = PREDEFINED_LAYOUTS[id]
  if (!builtIn || settings?.layouts_enabled?.[id] === false) return null
  return builtIn
}

const cell = (row: number, col: number, span = 1): LayoutTile => ({ row, col, rowSpan: span, colSpan: span })

/** Every 1×1 cell of a cols×rows grid, row by row, skipping those `taken`. */
function cells(cols: number, rows: number, taken: (r: number, c: number) => boolean = () => false): LayoutTile[] {
  const out: LayoutTile[] = []
  for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) if (!taken(r, c)) out.push(cell(r, c))
  return out
}

export const PREDEFINED_LAYOUTS: Record<string, LayoutDefinition> = {
  '1x1': { name: '1×1', description: '1 camera full screen', gridCols: 1, gridRows: 1, tiles: cells(1, 1) },
  '2x2': { name: '2×2', description: '4 cameras in 2×2 grid', gridCols: 2, gridRows: 2, tiles: cells(2, 2) },
  '3x3': { name: '3×3', description: '9 cameras in 3×3 grid', gridCols: 3, gridRows: 3, tiles: cells(3, 3) },
  '4x4': { name: '4×4', description: '16 cameras in 4×4 grid', gridCols: 4, gridRows: 4, tiles: cells(4, 4) },
  '1+5': {
    name: '1+5',
    description: '1 large + 5 small cameras',
    gridCols: 3,
    gridRows: 3,
    tiles: [cell(0, 0, 2), ...cells(3, 3, (r, c) => r < 2 && c < 2)],
  },
  '1+7': {
    name: '1+7',
    description: '1 large + 7 small cameras',
    gridCols: 4,
    gridRows: 4,
    tiles: [cell(0, 0, 3), ...cells(4, 4, (r, c) => r < 3 && c < 3)],
  },
  '2+8': {
    name: '2+8',
    description: '2 large + 8 small cameras',
    gridCols: 4,
    gridRows: 4,
    tiles: [cell(0, 0, 2), cell(0, 2, 2), ...cells(4, 4, (r) => r < 2)],
  },
  // 2×2 large top-left; the other 12 cells of a 4×4 grid are small.
  '1+12': {
    name: '1+12',
    description: '1 large + 12 small cameras',
    gridCols: 4,
    gridRows: 4,
    tiles: [cell(0, 0, 2), ...cells(4, 4, (r, c) => r < 2 && c < 2)],
  },
  '4+9': {
    name: '4+9',
    description: '4 medium + 9 small cameras',
    gridCols: 5,
    gridRows: 5,
    tiles: [cell(0, 0, 2), cell(0, 2, 2), cell(2, 0, 2), cell(2, 2, 2), ...cells(5, 5, (r, c) => r < 4 && c < 4)],
  },
  // Two 2×2 large side by side; a 2×2 block of small to their right and a
  // row of six beneath.
  '1+1+10': {
    name: '1+1+10',
    description: '2 large side-by-side + 10 small',
    gridCols: 6,
    gridRows: 3,
    tiles: [cell(0, 0, 2), cell(0, 2, 2), ...cells(6, 3, (r, c) => r < 2 && c < 4)],
  },
}
