
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { clsx } from 'clsx'
import { ErrorCard, Skeleton, TBody, TD, TH, THead, TR } from './index'
import { extractApiError } from '../../lib/apiError'

/**
 * A column, not a cell. Four things have to agree across the header and
 * every body cell — the colSpan for the loading/empty rows, responsive
 * hiding, alignment, and the skeleton's shape — and they can only agree
 * if they are derived from one description of the column.
 *
 * NOTE for future readers: this is deliberately not @tanstack/react-table.
 * That library's value is headless sorting/filtering/grouping, all of
 * which happens on the server here, so it would add a whole peer concept
 * for a component this size. It is not a dependency; please do not make
 * it one on this component's account.
 */
export type Column<T> = {
  key: string
  header: ReactNode
  cell: (row: T, index: number) => ReactNode
  align?: 'left' | 'right' | 'center'
  /** Tailwind width class, applied to the header cell. */
  width?: string
  /**
   * Applied to the header AND every body cell — for anything structural
   * that must match on both, like width, alignment or responsive hiding.
   * NOT for colour or type: a cell's dim body text leaking onto the
   * header made that one header read differently from its neighbours.
   */
  className?: string
  /** Body cells only — colour, weight, numerals, truncation. */
  cellClassName?: string
  /** Hide below this breakpoint. */
  hideBelow?: 'sm' | 'md' | 'lg'
  /** For action columns: a visually empty header that still announces. */
  srHeader?: string
  /**
   * This cell holds controls. A click inside it acts on the control and
   * must NOT also activate the row — without this, hitting Ack on a
   * row-clickable table would fire the row's own handler as the event
   * bubbled.
   */
  isAction?: boolean
}

// Static strings: Tailwind v4 scans source text, so a class built by
// concatenation would be purged out of the stylesheet.
const HIDE_BELOW: Record<'sm' | 'md' | 'lg', string> = {
  sm: 'hidden sm:table-cell',
  md: 'hidden md:table-cell',
  lg: 'hidden lg:table-cell',
}

const ALIGN: Record<'left' | 'right' | 'center', string> = {
  left: 'text-left',
  right: 'text-right',
  center: 'text-center',
}

export type DataTableProps<T> = {
  columns: Column<T>[]
  rows: T[]
  rowKey: (row: T) => string | number
  /** Required: rendered as an sr-only <caption>. */
  caption: string
  isPending?: boolean
  isFetching?: boolean
  isError?: boolean
  error?: unknown
  errorTitle?: string
  onRetry?: () => void
  /** Shown in place of rows when there are none. */
  empty?: ReactNode
  skeletonRows?: number
  striped?: boolean
  rowClassName?: (row: T) => string
  /** Rendered inside the bordered shell, under the table. */
  footer?: ReactNode
  /**
   * Rendered inside the shell ABOVE the rows — where the pagination
   * lives, beside the filters it pages through, and visible without
   * scrolling to the end of a long page.
   */
  toolbar?: ReactNode
  /**
   * Scroll the ROWS instead of the page: the body takes whatever height
   * is left below it in the viewport and scrolls inside that, so the
   * header stays put and the page does not grow a long outer scrollbar.
   */
  fillHeight?: boolean
  /** Never shrink the scroller below this (px). */
  minBodyHeight?: number
  /**
   * `table-layout: fixed` — column widths are obeyed instead of being
   * derived from content. Pair it with a width on every column but one
   * (the flexible one) and with `truncate` on any cell that can overflow:
   * fixed layout WRAPS rather than expands, and a wrapped cell makes the
   * row taller than its neighbours.
   */
  fixed?: boolean
  /** Minimum table width before the scroller scrolls sideways. */
  minWidth?: string
  /**
   * Tighter vertical padding, for tables whose job is to show as many
   * rows as possible. Applied through the table element rather than by
   * changing the shared TD/TH primitives, so the app's default density
   * is untouched everywhere else.
   */
  dense?: boolean
  /**
   * Makes the whole row activatable, mail-client style. Rows get a
   * pointer, a keyboard focus ring and Enter/Space handling — not just
   * an onClick, which would be unreachable without a mouse.
   */
  onRowClick?: (row: T) => void
}

/**
 * The height left between the scroller's top edge and the bottom of the
 * window, minus whatever sits inside the shell below the rows.
 *
 * Measured rather than hard-coded: the distance from the viewport top
 * changes with the filter bar wrapping, the stat tiles, and the page
 * header, and a magic `calc(100vh - 420px)` would be wrong on the first
 * layout that differs from the one it was tuned against.
 */
function useAvailableHeight(
  enabled: boolean,
  scrollerRef: React.RefObject<HTMLDivElement | null>,
  footerRef: React.RefObject<HTMLDivElement | null>,
  minHeight: number,
) {
  const [maxHeight, setMaxHeight] = useState<number>()

  const measure = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    // Document-absolute on purpose, and it is scroll-INVARIANT: scroll
    // down 200px and rect.top falls by 200 while scrollY rises by 200.
    // Read the whole expression as "how tall may I be so the page does
    // not scroll at all", which is this component's entire job.
    const top = el.getBoundingClientRect().top + window.scrollY
    // Walk from the SHELL, not the scroller. The pagination footer is the
    // scroller's next sibling but the shell's child, so starting one
    // level up skips it exactly once — it is already counted via
    // footerRef. Counting it twice is what left ~100px of dead page
    // under the table while the rows starved.
    const shell = el.parentElement ?? el
    const reserved = (footerRef.current?.offsetHeight ?? 0) + heightBelow(shell)
    // clientHeight, NOT innerHeight: innerHeight includes the horizontal
    // scrollbar gutter, so on any page that scrolls sideways it
    // over-reports the layout viewport by ~15px — the table then claims
    // 15px it does not have and the page grows a vertical scrollbar,
    // which is the exact thing this is here to prevent. The extra pixel
    // absorbs sub-pixel layout rounding.
    const viewport = document.documentElement.clientHeight || window.innerHeight
    const next = Math.max(minHeight, viewport - top - reserved - 1)
    // Only commit a real change: writing the same number every time an
    // observer fires would re-render on a loop.
    setMaxHeight((prev) => (prev !== undefined && Math.abs(prev - next) < 2 ? prev : next))
  }, [scrollerRef, footerRef, minHeight])

  useLayoutEffect(() => {
    if (!enabled) { setMaxHeight(undefined); return }
    measure()
  }, [enabled, measure])

  useEffect(() => {
    if (!enabled) return
    window.addEventListener('resize', measure)
    // Anything above the table changing height moves our top edge — a
    // wrapping filter bar, a stat tile loading, the sidebar collapsing.
    const ro = typeof ResizeObserver !== 'undefined'
      ? new ResizeObserver(measure)
      : null
    ro?.observe(document.body)
    return () => {
      window.removeEventListener('resize', measure)
      ro?.disconnect()
    }
  }, [enabled, measure])

  return maxHeight
}

/**
 * How much page sits BELOW the table — a card, a note, an ancestor's
 * bottom padding — so the rows take the rest and the page itself never
 * grows a scrollbar.
 *
 * Call it with the table's SHELL, not its scroller: the pagination
 * footer lives inside the shell and is measured separately, and walking
 * from the scroller counted it a second time.
 *
 * Two things `offsetHeight` alone would miss, both of which showed up as
 * the table overhanging its space:
 *  - margins, so anything spaced by `space-y-*` was under-counted;
 *  - an ancestor's bottom padding, which no sibling walk can see (the
 *    app shell's own `p-4` is 16px of it). Measuring it is what let the
 *    hard-coded BOTTOM_GUTTER guess go away.
 *
 * Out-of-flow elements are skipped — a portalled modal or a fixed
 * toolbar takes no space in the page and must not steal any from rows.
 */
function heightBelow(start: Element): number {
  let total = 0
  let node: Element | null = start
  while (node && node !== document.body) {
    for (let sib = node.nextElementSibling; sib; sib = sib.nextElementSibling) {
      const cs = window.getComputedStyle(sib)
      if (cs.position === 'fixed' || cs.position === 'absolute') continue
      total += sib.getBoundingClientRect().height
        + (parseFloat(cs.marginTop) || 0)
        + (parseFloat(cs.marginBottom) || 0)
    }
    const parent: HTMLElement | null = node.parentElement
    if (parent) {
      total += parseFloat(window.getComputedStyle(parent).paddingBottom) || 0
    }
    node = parent
  }
  return total
}

/**
 * The state machine here is the point, not the markup.
 *
 * Error takes precedence over empty, and `isError` is part of the props
 * type rather than something a caller opts into. Two of the three tables
 * this replaced never read their query's error flag at all, so a failed
 * fetch rendered as "No alarms yet" — indistinguishable from a working
 * system with nothing to report. Making the failure state structural is
 * what stops that being rewritten by hand each time.
 */
export function DataTable<T>({
  columns, rows, rowKey, caption,
  isPending = false, isFetching = false, isError = false, error,
  errorTitle = 'Could not load this list', onRetry,
  empty, skeletonRows = 8, striped = true, rowClassName, footer, toolbar,
  fillHeight = false, minBodyHeight = 220, fixed = false, minWidth,
  onRowClick, dense = false,
}: DataTableProps<T>) {
  const scrollerRef = useRef<HTMLDivElement>(null)
  const footerRef = useRef<HTMLDivElement>(null)
  const maxHeight = useAvailableHeight(fillHeight, scrollerRef, footerRef, minBodyHeight)
  // `width` belongs on BOTH cells. It used to be spread into the <TH>
  // only, so nothing constrained the body and `table-layout: auto`
  // sprayed the surplus into whichever columns had the longest content —
  // which is why PLATE/CAMERA/SEEN floated in whitespace with the row
  // actions stranded at the far edge.
  const cls = (c: Column<T>) => clsx(
    c.className,
    c.width,
    c.align && ALIGN[c.align],
    c.hideBelow && HIDE_BELOW[c.hideBelow],
  )

  if (isError) {
    return (
      <ErrorCard
        title={errorTitle}
        message={extractApiError(error, 'Please try again.')}
        onRetry={onRetry}
      />
    )
  }

  const head = (
    // Sticky ONLY under `fillHeight`. Sticky positions against the
    // nearest SCROLLING ancestor, so in a horizontal-only scroller there
    // is nothing to stick to and the header just does not move — which
    // is exactly what shipped the first time this was attempted. Give
    // the same container a height and a vertical scroller and it works.
    <THead>
      <TR className="hover:bg-transparent border-b border-[var(--border)]">
        {columns.map((c) => (
          <TH
            key={c.key}
            className={clsx(
              // A header has to out-rank the data or it stops reading as
              // one. Uppercase at 11px in the dim token was doing that
              // job through SHOUTING; plain 11px dim did not do it at
              // all — it came out weaker than the rows beneath it.
              // Weight and contrast instead: 12px semibold in the full
              // text colour is unmistakably a label, and quiet, because
              // it is smaller than the data it sits over.
              cls(c), 'text-xs font-semibold normal-case tracking-normal text-[var(--text)]',
              // The background is load-bearing, not decoration: rows
              // scroll UNDER this, and a transparent header would show
              // them through. The inset shadow keeps a divider visible
              // where a collapsed border would be painted away.
              fillHeight && 'sticky top-0 z-10 bg-[var(--bg-2)] shadow-[inset_0_-1px_0_var(--border)]',
            )}
          >
            {c.srHeader ? <span className="sr-only">{c.srHeader}</span> : c.header}
          </TH>
        ))}
      </TR>
    </THead>
  )

  return (
    // The shell is built here rather than with the shared `Table`
    // wrapper because the footer has to sit INSIDE the border with the
    // rows, and `Table` closes its own box around the table alone.
    // Deliberately no `overflow-hidden` on the shell: it would clip the
    // scroller it contains, which is the exact bug the Alerts &
    // Incidents table shipped with.
    <div className="border border-[var(--border)] rounded">
      {toolbar && (
        <div className="border-b border-[var(--border)]">{toolbar}</div>
      )}
      <div
        ref={scrollerRef}
        // thin-scroll is the app's existing panel scrollbar (index.css):
        // 6px, rounded, themed, and always faintly visible. Its reason
        // for an always-on thumb — content with no hover affordance to
        // tell you it scrolls — is exactly a table's situation, and the
        // default chrome scrollbar looked bolted on next to it.
        className={clsx('overflow-x-auto thin-scroll', fillHeight && 'overflow-y-auto')}
        style={fillHeight && maxHeight ? { maxHeight } : undefined}
      >
        <table className={clsx(
          'w-full text-sm', fixed && 'table-fixed', minWidth,
          dense && '[&_td]:py-1.5 [&_th]:py-1.5',
        )}>
        <caption className="sr-only">{caption}</caption>
        {head}
        {/* Keeps the frame and the footer mounted through every state, so
            the page does not jump as a filter empties the list. */}
        <TBody
          striped={striped && !isPending}
          aria-busy={isFetching && !isPending ? true : undefined}
          className={clsx(isFetching && !isPending && 'opacity-70 transition-opacity')}
        >
          {isPending
            ? Array.from({ length: skeletonRows }).map((_, i) => (
                <TR key={`sk${i}`} className="hover:bg-transparent">
                  {columns.map((c) => (
                    <TD key={c.key} className={cls(c)}><Skeleton className="h-4" /></TD>
                  ))}
                </TR>
              ))
            : rows.length === 0
              ? (
                <TR className="hover:bg-transparent">
                  <TD colSpan={columns.length} className="py-8">{empty}</TD>
                </TR>
              )
              : rows.map((row, i) => (
                <TR
                  key={rowKey(row)}
                  className={clsx(onRowClick && 'cursor-pointer', rowClassName?.(row))}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  tabIndex={onRowClick ? 0 : undefined}
                  onKeyDown={onRowClick ? (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      onRowClick(row)
                    }
                  } : undefined}
                >
                  {columns.map((c) => (
                    <TD
                      key={c.key}
                      className={clsx(cls(c), c.cellClassName)}
                      onClick={c.isAction ? (e) => e.stopPropagation() : undefined}
                    >
                      {c.cell(row, i)}
                    </TD>
                  ))}
                </TR>
              ))}
        </TBody>
        </table>
      </div>
      <div ref={footerRef} className={clsx(footer && 'border-t border-[var(--border)]')}>
        {footer}
      </div>
    </div>
  )
}
