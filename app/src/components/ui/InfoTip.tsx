import { useId, type ReactNode } from 'react'
import { Info } from 'lucide-react'

/**
 * The detail behind a one-line summary, shown on hover or keyboard focus.
 *
 * Settings cards used to carry three lines of explanation each, so text
 * read once outweighed controls used daily. The summary stays on the
 * page; the rest is one hover away, and still reaches a screen reader
 * through aria-describedby (a hidden element can describe a visible one).
 *
 * Anchored to the LEFT edge of the icon, not centred: these icons sit
 * beside titles near the left of a card, where a centred tip would run
 * off the edge.
 */
export function InfoTip({
  children, label = 'More information',
}: {
  children: ReactNode
  /** Accessible name for the icon button. */
  label?: string
}) {
  const id = useId()
  return (
    <span className="group relative inline-flex">
      <button
        type="button"
        aria-label={label}
        aria-describedby={id}
        className="rounded-full text-[var(--text-dim)] hover:text-[var(--text)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
      >
        <Info size={13} />
      </button>
      <span
        id={id}
        role="tooltip"
        className="pointer-events-none invisible absolute left-0 top-full z-30 mt-1.5 w-72 max-w-[80vw] rounded border border-[var(--border)] bg-[var(--panel)] px-3 py-2 text-xs font-normal leading-relaxed text-[var(--text)] opacity-0 shadow-lg transition-opacity group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100"
      >
        {children}
      </span>
    </span>
  )
}
