import { useId, useState, type ReactNode, type SyntheticEvent } from 'react'
import { Info } from 'lucide-react'

/** w-72 below. */
const TIP_WIDTH_PX = 288

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
 * off the edge. An icon too near the window's right edge anchors to its
 * right instead. Out of layout until shown (display, not visibility): a hidden
 * tip past the edge would otherwise give the page a sideways scroll.
 */
export function InfoTip({
  children, label = 'More information',
}: {
  children: ReactNode
  /** Accessible name for the icon button. */
  label?: string
}) {
  const id = useId()
  // Near the right of the window the tip opens leftwards instead of off-screen.
  const [flip, setFlip] = useState(false)
  const place = (e: SyntheticEvent<HTMLElement>) =>
    setFlip(e.currentTarget.getBoundingClientRect().left + TIP_WIDTH_PX + 8 > window.innerWidth)
  return (
    <span className="group relative inline-flex" onMouseEnter={place} onFocus={place}>
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
        className={`pointer-events-none hidden absolute ${flip ? 'right-0' : 'left-0'} top-full z-30 mt-1.5 w-72 max-w-[80vw] rounded-[var(--radius)] border border-[var(--border)] bg-[var(--panel)] px-3 py-2 text-xs font-normal leading-relaxed text-[var(--text)] shadow-lg group-hover:block group-focus-within:block`}
      >
        {children}
      </span>
    </span>
  )
}
