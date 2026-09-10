import type { ReactNode } from 'react'
import { clsx } from 'clsx'

export type SegmentOption<V> = {
  value: V
  label: ReactNode
  title?: string
}

/**
 * One choice from a small fixed set — a time range, a severity, a status.
 *
 * ONE CHANNEL PER MEANING: the fill says "chosen", the text says "under
 * the pointer". The selected segment gets a tint and an accent hairline;
 * the others only brighten their text on hover. The alarm status filter
 * used to be a toggle button whose pressed look WAS the outline button's
 * hover look, so pointing at it and having switched it on were
 * indistinguishable.
 *
 * Not a solid accent fill — that competed with the page's one primary
 * button. And no weight change on selection: bold text is wider, so the
 * segments shuffled sideways under the pointer on every click.
 */
export function SegmentedControl<V extends string | number | null>({
  label, showLabel = false, options, value, onChange,
}: {
  /** Names the group for assistive tech; shown too when `showLabel`. */
  label: string
  /** Show the label beside the group — needed when two groups sit side
   *  by side and both start with "All". */
  showLabel?: boolean
  options: SegmentOption<V>[]
  value: V
  onChange: (value: V) => void
}) {
  const group = (
    <div
      role="group"
      aria-label={label}
      className="flex divide-x divide-[var(--border)] overflow-hidden rounded border border-[var(--border)]"
    >
      {options.map((o) => {
        const on = o.value === value
        return (
          <button
            key={String(o.value)}
            type="button"
            aria-pressed={on}
            title={o.title}
            // Choosing the current option again is a no-op, not a toggle.
            onClick={() => { if (!on) onChange(o.value) }}
            className={clsx(
              'whitespace-nowrap px-2.5 py-1 text-xs transition-colors',
              'focus-visible:relative focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[var(--accent)]',
              on
                ? 'cursor-default bg-[var(--accent)]/15 text-[var(--text)] shadow-[inset_0_0_0_1px_var(--accent)]'
                : 'cursor-pointer text-[var(--text-dim)] hover:text-[var(--text)]',
            )}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
  if (!showLabel) return group
  return (
    <div className="flex items-center gap-2">
      {/* A caption, not a control: the app's small-caps label style, faded
          behind the options. Same size and colour as an unselected option,
          it read as one more button to press. Hidden from assistive tech —
          the group's aria-label says it. */}
      <span aria-hidden className="select-none text-[11px] uppercase tracking-wider text-[var(--text-dim)] opacity-80">
        {label}
      </span>
      {group}
    </div>
  )
}
