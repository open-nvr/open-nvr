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
 * LOCAL-day time helpers, shared by every playback view.
 *
 * A "day" in the recordings UI is the browser's local calendar day —
 * midnight to midnight in the viewer's timezone — matching the backend's
 * local-day grouping (`/recordings/list?tz=...`) and the local-time-named
 * storage layout. Nothing here uses UTC dates; the API receives explicit
 * instants (or a date plus the IANA zone name) instead.
 */

/** Epoch ms of local midnight beginning `date` (YYYY-MM-DD). */
export function localDayStart(date: string): number {
  const [y, m, d] = date.split('-').map(Number)
  return new Date(y, m - 1, d).getTime()
}

/** Epoch ms of the local midnight AFTER `date` (start of the next day). */
export function localDayEnd(date: string): number {
  const [y, m, d] = date.split('-').map(Number)
  return new Date(y, m - 1, d + 1).getTime()
}

/** Today's local date key (YYYY-MM-DD). */
export function todayLocalKey(): string {
  return localDateKey(new Date())
}

/** Local date key (YYYY-MM-DD) of a Date or epoch ms. */
export function localDateKey(t: Date | number): string {
  const d = typeof t === 'number' ? new Date(t) : t
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/** The browser's IANA timezone name, sent to the API for day grouping. */
export function browserTz(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}
