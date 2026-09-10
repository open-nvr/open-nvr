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

/** Human-readable footage duration: "46h 12m", or "32m" under an hour. */
export function formatDuration(seconds: number): string {
  const hours = Math.floor(seconds / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  if (hours > 0) {
    return `${hours}h ${mins}m`
  }
  return `${mins}m`
}

/** The browser's IANA timezone name, sent to the API for day grouping. */
export function browserTz(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

/**
 * A timestamp for a dense operator table: `10 Sep 06:50:23`, carrying the
 * year only when it is not the current one.
 *
 * Always dated. An earlier version dropped the date for today's rows to
 * save width, which read fine at a glance and badly in practice — a row
 * showing `06:50:23` gives no clue whether it is from this morning or
 * three weeks ago once the range is 7 or 30 days, and this column is
 * exported as evidence. Pair it with `tabular-nums` so the digits line
 * up down the column.
 *
 * Returns '' for a missing/unparseable value; callers show their own
 * placeholder. `seenAtTitle` gives the full localised value for the
 * cell's tooltip.
 */
export function formatSeenAt(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const time = d.toLocaleTimeString(undefined, {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
  const date = d.toLocaleDateString(undefined, {
    day: '2-digit',
    month: 'short',
    ...(d.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
  })
  return `${date} ${time}`
}

/** The unabbreviated timestamp, for the tooltip on a compact cell. */
export function seenAtTitle(iso: string | null | undefined): string | undefined {
  if (!iso) return undefined
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? undefined : d.toLocaleString()
}
