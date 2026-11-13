// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * One icon per app page, keyed by route: the sidebar and each app page's
 * title bar draw the same glyph, so the page you land on looks like the
 * link you clicked.
 */

import type { ReactNode } from 'react'
import {
  BellRing, Boxes, Briefcase, Car, DoorOpen, GitCommitHorizontal, Hourglass,
  PackageCheck, ShieldAlert, ShieldCheck, UserRound, Users,
} from 'lucide-react'

const ICONS: Record<string, (size: number) => ReactNode> = {
  '/vehicles': (s) => <Car size={s} />,
  '/occupancy': (s) => <Users size={s} />,
  '/guard-compliance': (s) => <ShieldCheck size={s} />,
  '/people': (s) => <UserRound size={s} />,
  '/tripwires': (s) => <GitCommitHorizontal size={s} />,
  '/loitering': (s) => <Hourglass size={s} />,
  '/perimeter': (s) => <ShieldAlert size={s} />,
  '/left-items': (s) => <Briefcase size={s} />,
  '/deliveries': (s) => <PackageCheck size={s} />,
  // lucide has no barrier/boom-gate glyph; DoorOpen is the nearest
  // thing that reads as "a way through that opens".
  '/gates': (s) => <DoorOpen size={s} />,
  // Bell is already the alert inbox in the nav; BellRing is the one that
  // reads as "it actually went off on someone's phone".
  '/notifications': (s) => <BellRing size={s} />,
}

/** The route's icon, or a generic app glyph for a page not listed here. */
export function verticalIcon(route: string, size = 16): ReactNode {
  return (ICONS[route] ?? ((s: number) => <Boxes size={s} />))(size)
}
