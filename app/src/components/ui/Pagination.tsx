import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react'
import { Button } from './index'

export type PaginationProps = {
  page: number
  pageSize: number
  /** Rows matching the filters. Undefined when the server did not say. */
  total?: number
  /** Only consulted when `total` is undefined. */
  hasNext?: boolean
  /** Rows on the current page, for an honest range label. */
  rowCount?: number
  pageSizeOptions?: number[]
  onPageChange: (page: number) => void
  onPageSizeChange: (size: number) => void
  isFetching?: boolean
  /** Plural noun for the range label: "1–25 of 312 reads". */
  label?: string
  /** Only one instance on a page should be a live region. */
  announce?: boolean
}

export function Pagination({
  page, pageSize, total, hasNext, rowCount,
  pageSizeOptions = [25, 50, 100],
  onPageChange, onPageSizeChange, isFetching = false, label,
  announce = true,
}: PaginationProps) {
  const knownTotal = typeof total === 'number'
  const shown = rowCount ?? 0
  const from = shown ? (page - 1) * pageSize + 1 : 0
  const to = shown ? from + shown - 1 : 0
  const totalPages = knownTotal ? Math.max(1, Math.ceil(total / pageSize)) : undefined
  const noun = label ? ` ${label}` : ''

  // With a total, Next is arithmetic. Without one, only the caller can
  // say — a full page is the hint, never a guess dressed up as a count.
  const canPrev = page > 1 && !isFetching
  const canNext = !isFetching && (totalPages ? page < totalPages : Boolean(hasNext))

  const rangeText = !knownTotal
    ? (shown ? `Showing ${from}–${to}` : 'No results')
    : total === 0
      ? `No${noun ? noun : ' results'}`
      : `${from}–${to} of ${total}${noun}`

  return (
    // Everything sits right, reading count -> pages -> page size: what
    // you are looking at, how to move, then how much to show.
    //
    // Prev/next only. Numbered pages are a lot of chrome for a list read
    // newest-first, where "older" and "newer" is the whole interaction —
    // the count says where you are and the chevrons move you. It also
    // ends the strip growing wider as the result set does.
    <nav
      aria-label="Pagination"
      className="flex flex-wrap items-center justify-end gap-x-3 gap-y-2 px-3 py-1.5 text-xs"
    >
      {/* Only ONE of the two instances announces, or a screen reader
          hears the same range twice on every page change. */}
      <span
        role={announce ? 'status' : undefined}
        aria-live={announce ? 'polite' : undefined}
        className="text-[var(--text-dim)]"
      >
        {rangeText}
      </span>

      <div className="flex items-center gap-0.5">
        {/* Chevrons, not words. The arrows need no translation and no
            reading — and an icon-only control still needs a real name,
            hence aria-label. Doubled chevrons jump to the ends, which is
            what the numbered strip was really being used for. */}
        <Button
          variant="ghost" size="sm" aria-label="First page" title="First page"
          className="px-1.5" disabled={!canPrev} onClick={() => onPageChange(1)}
        >
          <ChevronsLeft size={16} />
        </Button>
        <Button
          variant="ghost" size="sm" aria-label="Previous page" title="Previous page"
          className="px-1.5" disabled={!canPrev} onClick={() => onPageChange(page - 1)}
        >
          <ChevronLeft size={16} />
        </Button>
        <Button
          variant="ghost" size="sm" aria-label="Next page" title="Next page"
          className="px-1.5" disabled={!canNext} onClick={() => onPageChange(page + 1)}
        >
          <ChevronRight size={16} />
        </Button>
        {/* Only when the server told us the total — without it there is
            no last page to jump to, and a control that guesses one would
            be worse than none. */}
        {totalPages !== undefined && (
          <Button
            variant="ghost" size="sm" aria-label="Last page" title={`Last page (${totalPages})`}
            className="px-1.5" disabled={!canNext} onClick={() => onPageChange(totalPages)}
          >
            <ChevronsRight size={16} />
          </Button>
        )}
      </div>

      {/* Borderless too — it is a preference, not a field to fill in. */}
      <label className="flex items-center text-[var(--text-dim)]">
        <span className="sr-only">Rows per page</span>
        <select
          className="cursor-pointer rounded border-0 bg-transparent py-0.5 pl-1 pr-0 text-xs text-[var(--text-dim)] hover:text-[var(--text)]"
          value={pageSize}
          onChange={(e) => onPageSizeChange(Number(e.target.value))}
        >
          {pageSizeOptions.map((n) => (
            <option key={n} value={n}>{n} / page</option>
          ))}
        </select>
      </label>
    </nav>
  )
}
