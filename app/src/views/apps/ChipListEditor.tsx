/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Chip/tag editor for App SDK `list` (of strings) config params — plate
 * watchlists, watched labels, and the like. Replaces the raw JSON
 * textarea: an operator types a value + Enter (or comma) to add a chip,
 * clicks × to remove one. The stored value stays a JSON array string so
 * the config-modal submit path (JSON.parse) is unchanged.
 */
import { useMemo, useState } from 'react'

// Parse the stored value (JSON array string, or already an array) into a
// string[]; tolerate junk rather than throwing.
function parseChips(raw: string): string[] {
  if (!raw || !raw.trim()) return []
  try {
    const v = JSON.parse(raw)
    if (Array.isArray(v)) return v.map((x) => String(x))
  } catch {
    // A comma/newline list that isn't valid JSON yet — best-effort split.
    return raw.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
  }
  return []
}

export function ChipListEditor({
  value,
  onChange,
  placeholder,
  transform,
  suggestions,
  suggestionsLabel,
}: {
  value: string
  onChange: (json: string) => void
  placeholder?: string
  // Optional normalization applied to each added chip (e.g. plates →
  // upper). Kept UI-side convenience only; the app re-normalizes too.
  transform?: (s: string) => string
  // One-click values (a label vocabulary, plate formats…) shown under
  // the input; already-chosen ones are hidden. Purely additive — the
  // operator can still type anything.
  suggestions?: string[]
  suggestionsLabel?: string
}) {
  const chips = useMemo(() => parseChips(value), [value])
  const [draft, setDraft] = useState('')

  const write = (next: string[]) => onChange(JSON.stringify(next))
  const addOne = (s: string) => {
    const v = transform ? transform(s) : s
    if (v && !chips.includes(v)) write([...chips, v])
  }
  const offered = (suggestions ?? []).filter((s) => s && !chips.includes(transform ? transform(s) : s))

  const addDraft = () => {
    // Support pasting a comma/newline-separated batch in one go.
    const parts = draft
      .split(/[\n,]/)
      .map((s) => (transform ? transform(s.trim()) : s.trim()))
      .filter(Boolean)
    if (parts.length === 0) return
    const merged = [...chips]
    for (const p of parts) if (!merged.includes(p)) merged.push(p)
    write(merged)
    setDraft('')
  }

  const removeAt = (i: number) => write(chips.filter((_, k) => k !== i))

  return (
    <div className="rounded border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1.5">
      <div className="flex flex-wrap gap-1.5">
        {chips.map((c, i) => (
          <span
            key={`${c}-${i}`}
            className="inline-flex items-center gap-1 text-xs rounded bg-[var(--bg-3,var(--bg-1))] border border-[var(--border)] px-1.5 py-0.5"
          >
            <span className="font-mono">{c}</span>
            <button
              type="button"
              className="text-[var(--text-dim)] hover:text-red-400 leading-none"
              onClick={() => removeAt(i)}
              aria-label={`remove ${c}`}
            >
              ×
            </button>
          </span>
        ))}
        <input
          className="flex-1 min-w-[8rem] bg-transparent text-sm outline-none"
          value={draft}
          placeholder={chips.length === 0 ? (placeholder || 'type a value, Enter to add') : ''}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ',') {
              e.preventDefault()
              addDraft()
            } else if (e.key === 'Backspace' && !draft && chips.length) {
              removeAt(chips.length - 1)
            }
          }}
          onBlur={addDraft}
        />
      </div>
      {offered.length > 0 && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1 text-[11px] text-[var(--text-dim)]">
          <span>{suggestionsLabel ?? 'Suggestions:'}</span>
          {offered.slice(0, 12).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => addOne(s)}
              className="font-mono rounded border border-dashed border-[var(--border)] px-1.5 py-0.5 hover:border-[var(--accent)] hover:text-[var(--text)]"
              title={`add ${s}`}
            >
              + {s}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
