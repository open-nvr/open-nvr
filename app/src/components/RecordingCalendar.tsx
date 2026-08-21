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

import { useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'

interface RecordingCalendarProps {
  /** Selected day, YYYY-MM-DD (same UTC-day strings the recordings API returns). */
  selected: string | null
  onSelect: (date: string) => void
  /** Days that have footage — get a marker and stronger text. */
  markedDates: Set<string>
  className?: string
}

const WEEKDAYS = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa']
const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

/** Format y/m(0-based)/d as YYYY-MM-DD without touching timezones. */
const key = (y: number, m: number, d: number) =>
  `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`

function parseKey(s: string | null): { y: number; m: number } | null {
  if (!s) return null
  const [y, m] = s.split('-').map(Number)
  if (!Number.isFinite(y) || !Number.isFinite(m)) return null
  return { y, m: m - 1 }
}

/**
 * Compact month calendar for picking a recording day (CP-Plus style): days
 * with footage are marked with an accent dot, the selected day is filled.
 * Day keys are plain date-string arithmetic — no Date/timezone conversion.
 */
export function RecordingCalendar({ selected, onSelect, markedDates, className = '' }: RecordingCalendarProps) {
  const today = new Date()
  const todayKey = key(today.getFullYear(), today.getMonth(), today.getDate())

  const init = parseKey(selected) ?? { y: today.getFullYear(), m: today.getMonth() }
  const [view, setView] = useState<{ y: number; m: number }>(init)
  // Re-center the visible month when the selection changes from outside
  // (e.g. "latest recording" auto-pick after data loads).
  const [lastSelected, setLastSelected] = useState(selected)
  if (selected !== lastSelected) {
    setLastSelected(selected)
    const p = parseKey(selected)
    if (p && (p.y !== view.y || p.m !== view.m)) setView(p)
  }

  const cells = useMemo(() => {
    const firstDow = new Date(view.y, view.m, 1).getDay()
    const daysInMonth = new Date(view.y, view.m + 1, 0).getDate()
    const daysInPrev = new Date(view.y, view.m, 0).getDate()
    const out: { y: number; m: number; d: number; outside: boolean }[] = []
    for (let i = firstDow - 1; i >= 0; i--) {
      const m = view.m === 0 ? 11 : view.m - 1
      const y = view.m === 0 ? view.y - 1 : view.y
      out.push({ y, m, d: daysInPrev - i, outside: true })
    }
    for (let d = 1; d <= daysInMonth; d++) out.push({ y: view.y, m: view.m, d, outside: false })
    let next = 1
    while (out.length % 7 !== 0) {
      const m = view.m === 11 ? 0 : view.m + 1
      const y = view.m === 11 ? view.y + 1 : view.y
      out.push({ y, m, d: next++, outside: true })
    }
    return out
  }, [view])

  const step = (dir: 1 | -1) =>
    setView((v) => {
      const m = v.m + dir
      if (m < 0) return { y: v.y - 1, m: 11 }
      if (m > 11) return { y: v.y + 1, m: 0 }
      return { y: v.y, m }
    })

  return (
    <div className={`select-none ${className}`}>
      {/* Month header */}
      <div className="flex items-center justify-between mb-1.5">
        <button
          onClick={() => step(-1)}
          className="p-1 text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel-2)]"
          aria-label="Previous month"
        >
          <ChevronLeft size={15} />
        </button>
        <span className="text-sm font-medium">
          {MONTHS[view.m]} {view.y}
        </span>
        <button
          onClick={() => step(1)}
          className="p-1 text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel-2)]"
          aria-label="Next month"
        >
          <ChevronRight size={15} />
        </button>
      </div>

      {/* Weekday header */}
      <div className="grid grid-cols-7 mb-0.5">
        {WEEKDAYS.map((w) => (
          <span key={w} className="text-center text-[10px] font-semibold text-[var(--text-dim)] py-0.5">
            {w}
          </span>
        ))}
      </div>

      {/* Day grid */}
      <div className="grid grid-cols-7 gap-y-0.5">
        {cells.map((c, i) => {
          const k = key(c.y, c.m, c.d)
          const marked = markedDates.has(k)
          const isSelected = k === selected
          const isToday = k === todayKey
          return (
            <button
              key={i}
              onClick={() => onSelect(k)}
              title={marked ? `${k} · has recordings` : k}
              className={`relative mx-auto w-7 h-7 flex items-center justify-center text-[11px] transition-colors ${
                isSelected
                  ? 'bg-[var(--accent)] text-white font-semibold'
                  : c.outside
                    ? 'text-[var(--text-dim)] opacity-40 hover:bg-[var(--panel-2)]'
                    : marked
                      ? 'text-[var(--text)] font-medium hover:bg-[var(--panel-2)]'
                      : 'text-[var(--text-dim)] hover:bg-[var(--panel-2)]'
              } ${isToday && !isSelected ? 'outline outline-1 outline-[var(--accent)]/60' : ''}`}
            >
              {c.d}
              {marked && !isSelected && (
                <span className="absolute bottom-0.5 left-1/2 -translate-x-1/2 w-1 h-1 rounded-full bg-[var(--accent)]" />
              )}
            </button>
          )
        })}
      </div>

      {/* Legend */}
      <div className="mt-1.5 flex items-center gap-3 text-[10px] text-[var(--text-dim)]">
        <span className="inline-flex items-center gap-1">
          <span className="w-1 h-1 rounded-full bg-[var(--accent)]" /> has recordings
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="w-2.5 h-2.5 outline outline-1 outline-[var(--accent)]/60" /> today
        </span>
      </div>
    </div>
  )
}
