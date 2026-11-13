/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Time-window picker for App SDK `time_range` config params (e.g.
 * intrusion-detection's restricted hours). Stores a `{start, end}`
 * object of "HH:MM" strings — the exact shape the apps already parse —
 * so the app side is unchanged; only the operator gets two clock
 * inputs instead of typing the range by hand. An overnight window
 * (start > end, e.g. 22:00-06:00) is normal and shown as such.
 */
import { useMemo } from 'react'

const HHMM = /^(\d{1,2}):(\d{2})$/

function normTime(t: unknown): string {
  const m = HHMM.exec(String(t ?? '').trim())
  if (!m) return ''
  const hh = Math.min(23, parseInt(m[1], 10))
  const mm = Math.min(59, parseInt(m[2], 10))
  return `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}`
}

// The stored value is a JSON object string {start, end} (config-modal
// submit JSON-parses time_range params). Tolerate an empty/garbage
// value by starting blank.
function parseRange(raw: string): { start: string; end: string } {
  try {
    const v = JSON.parse(raw)
    if (v && typeof v === 'object') return { start: normTime(v.start), end: normTime(v.end) }
  } catch {
    /* not JSON yet */
  }
  return { start: '', end: '' }
}

export function TimeWindowEditor({
  value,
  onChange,
}: {
  value: string
  onChange: (json: string) => void
}) {
  const { start, end } = useMemo(() => parseRange(value), [value])
  const overnight = start && end && start > end

  const write = (nextStart: string, nextEnd: string) =>
    onChange(JSON.stringify({ start: nextStart || '00:00', end: nextEnd || '00:00' }))

  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <input
        type="time"
        className="px-2 py-1 border border-[var(--border)] bg-[var(--bg-2)]"
        value={start}
        onChange={(e) => write(e.target.value, end)}
      />
      <span className="text-[var(--text-dim)]">to</span>
      <input
        type="time"
        className="px-2 py-1 border border-[var(--border)] bg-[var(--bg-2)]"
        value={end}
        onChange={(e) => write(start, e.target.value)}
      />
      {overnight && (
        <span className="text-xs text-[var(--text-dim)]">(overnight — spans midnight)</span>
      )}
      {start && end && start === end && (
        <span className="text-xs text-[var(--warn)]">(start = end → the whole day)</span>
      )}
    </div>
  )
}
