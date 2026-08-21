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

/**
 * Lazy, memoized loader for hls.js.
 *
 * hls.js is ~0.5MB — statically importing it made it a hard dependency of
 * every route chunk that touches video, blocking first render behind its
 * download. Consumers `await loadHls()` inside their (already async) session
 * setup instead, and views call `warmHls()` on mount so the chunk downloads
 * in parallel with their data fetching.
 */

import type HlsType from 'hls.js'

let hlsPromise: Promise<typeof HlsType> | null = null

export function loadHls(): Promise<typeof HlsType> {
  if (!hlsPromise) {
    hlsPromise = import('hls.js').then((m) => m.default)
  }
  return hlsPromise
}

/** Fire-and-forget prefetch; safe to call any number of times. */
export function warmHls(): void {
  loadHls().catch(() => {
    // Allow a retry on the next call instead of caching the failure forever.
    hlsPromise = null
  })
}
