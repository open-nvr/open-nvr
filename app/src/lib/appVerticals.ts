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
    capability: 'intrusion',
    to: '/perimeter',
    // The armed zones: what is armed right now, what is in alarm, and
    // the arm / disarm / bypass controls next to each. "Perimeter" is
    // the trade's word for the protected line and is wider than any one
    // app — the page is a panel, not a report.
    label: 'Perimeter',
    legacy: (m) => (m.requires_tasks ?? []).includes('intrusion_detection'),
  },
  {
    capability: 'left_items',
    to: '/left-items',
    // Every item left in a zone: who was with it, how long it has been
    // alone, and whether anyone came back. Named for the thing an
    // operator manages — "abandoned object" is the analytic, "left
    // items" is the queue they work through.
    label: 'Left Items',
    legacy: (m) => (m.requires_tasks ?? []).includes('abandoned_object'),
  },
  {
    capability: 'deliveries',
    to: '/deliveries',
    // What is waiting at each door, who brought it, who took it, and
    // today's drop-offs. Named for the thing an operator manages —
    // "package detection" is the analytic, "deliveries" is what a
    // homeowner or a front desk actually keeps track of.
    label: 'Deliveries',
    legacy: (m) => (m.requires_tasks ?? []).includes('package_delivery'),
  },
  {
    capability: 'gates',
    to: '/gates',
    // What every barrier is doing right now and the controls to open or
    // hold one. Named for the thing an operator manages — "barrier
    // control" is the mechanism, "gates" is what a guard at the kerb
    // actually talks about. A control surface, not a report.
    label: 'Gates',
    legacy: (m) => (m.requires_tasks ?? []).includes('gate_control'),
  },
  {
    capability: 'notifications',
    to: '/notifications',
    // Whether alerts are actually reaching anyone: the channels and
    // their health, the ordered routing rules, quiet hours and pauses,
    // and the reason any alert was held back. Named for the thing an
    // operator manages — "alert delivery" is the mechanism,
    // "notifications" is what they call the message on their phone.
    label: 'Notifications',
  },
  {
    capability: 'people',
    to: '/people',
    // The one row named for its app rather than for what the operator
    // manages, and deliberately so. "People (Faces)" told an operator
    // who had just installed Smart Doorbell nothing about where it went:
    // they went looking for the name on the tile they clicked and found
    // a word the catalog never used. The other rows do not have that
    // problem, because nobody installs an app called "Tripwires" — the
    // app is "Line Crossing" and the page is wider than it. Here the app
    // IS the page.
    //
    // If a second app ever provides `people`, this label stops being
    // true and should go back to the generic noun.
    label: 'Smart Doorbell',
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
