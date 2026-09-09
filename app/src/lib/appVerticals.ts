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

// The first-class verticals: capability → the page it lights up in the
// "Applications" nav group. ONE table, two consumers — the shell builds
// the nav from it, and the catalog tells an operator where an app they
// just enabled will appear. When those two disagree the catalog promises
// a menu entry that never arrives, so they must read the same row.
//
// Adding a vertical = one row here + its route + its page.

/** The shape both consumers need off a manifest; deliberately loose so
 *  callers can pass their own fuller manifest type. */
export type VerticalManifest = {
  provides?: string[] | null
  requires_tasks?: string[] | null
} | null | undefined

export type AppVertical = {
  /** manifest.provides entry that lights this page up. */
  capability: string
  /** Route, and the label shown under Applications. */
  to: string
  label: string
  /** Manifests that predate `provides` — matched on what they require
   *  instead. Kept so an install from before the field still gets its
   *  page (and its "you'll find it here" message). */
  legacy?: (m: NonNullable<VerticalManifest>) => boolean
}

export const APP_VERTICALS: AppVertical[] = [
  {
    capability: 'vehicles',
    to: '/vehicles',
    label: 'Vehicles',
    legacy: (m) => (m.requires_tasks ?? []).includes('license_plate_recognition'),
  },
  { capability: 'occupancy', to: '/occupancy', label: 'Occupancy' },
]

/** Does this manifest provide `vertical`? Capability first, legacy
 *  predicate second. */
export function manifestProvides(manifest: VerticalManifest, vertical: AppVertical): boolean {
  if (!manifest) return false
  if ((manifest.provides ?? []).includes(vertical.capability)) return true
  return vertical.legacy ? vertical.legacy(manifest) : false
}

/** The first-class page this app owns, or null if it has none — in which
 *  case the app's home is its own /app-catalog/<id> dashboard. */
export function verticalFor(manifest: VerticalManifest): AppVertical | null {
  return APP_VERTICALS.find((v) => manifestProvides(manifest, v)) ?? null
}
