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
    // "ANPR" is the internationally recognised name for this (UK, EU,
    // India, AU; "LPR"/"ALPR" is the US synonym) and it is the token the
    // catalog app carries too, so an operator can tell that the
    // "ANPR — License Plate Recognition" app they installed is what lit
    // this page up. "Vehicles" stays the head noun because the page is
    // wider than the OCR: plate reads, the vehicle register, monitoring
    // and alarms.
    label: 'Vehicles (ANPR)',
    legacy: (m) => (m.requires_tasks ?? []).includes('license_plate_recognition'),
  },
  { capability: 'occupancy', to: '/occupancy', label: 'Occupancy' },
  {
    capability: 'crossings',
    to: '/tripwires',
    // Counts and alarms per drawn line. "Tripwire" is the word the
    // security trade uses for this; the head noun stays generic because
    // the page is as much a footfall counter as a perimeter alarm.
    label: 'Tripwires',
  },
  {
    capability: 'loitering',
    to: '/loitering',
    // Who is dwelling in a drawn zone right now and for how long, the
    // stays and alarms per camera, and the dwell history. "Loitering"
    // is the trade's word; the page is as much a dwell-time report as an
    // alarm.
    label: 'Loitering',
  },
  {
    capability: 'people',
    to: '/people',
    // The face directory: who is enrolled, who the door just saw, and
    // enrolling a person from a snapshot the camera already took. Named
    // for what the operator manages, not for the model.
    label: 'People (Faces)',
    legacy: (m) => (m.requires_tasks ?? []).includes('face_recognition'),
  },
  {
    capability: 'guard_scan',
    to: '/guard-compliance',
    label: 'Entry Screening',
    // Named for what an operator watches, not for the model: the
    // page is about whether people are being screened properly.
    legacy: (m) => (m.requires_tasks ?? []).includes('pose_estimation'),
  },
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
