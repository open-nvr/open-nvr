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

// Has this element been on screen yet?
//
// Written for auth-gated images in long tables. A JWT-protected image cannot
// use <img loading="lazy"> — it has to be fetched as a blob — so every row
// mounting one fires a real request the moment the table paints. The Vehicles
// reads table renders up to 200 rows, and over HTTP/2 (no 6-per-host cap) that
// is ~200 concurrent requests, each holding a server-side DB connection for its
// duration. That measurably exhausted core's pool of 30.
//
// Latches ON: once seen, stay true. Evidence never changes, so re-hiding an
// image that is already fetched would only throw work away and make scrolling
// back up refetch.

import { useEffect, useRef, useState } from 'react'

export function useInView<T extends HTMLElement>(
  // A screen of lead-in, so images are already arriving by the time a row is
  // actually read rather than popping in under the cursor.
  rootMargin = '600px',
): [React.RefObject<T | null>, boolean] {
  const ref = useRef<T | null>(null)
  // No IntersectionObserver (old browser, jsdom) → behave exactly as before
  // this hook existed: eager. Degrading to "no images" would be worse than
  // degrading to "the old load pattern".
  const [inView, setInView] = useState(
    () => typeof IntersectionObserver === 'undefined',
  )

  useEffect(() => {
    if (inView) return
    const el = ref.current
    if (!el) return
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setInView(true)
          obs.disconnect()          // latched; nothing left to watch
        }
      },
      { rootMargin },
    )
    obs.observe(el)
    return () => obs.disconnect()
  }, [inView, rootMargin])

  return [ref, inView]
}
