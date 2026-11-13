import { useCallback, useMemo, useState } from 'react'

/** Where a table's chosen page size is remembered, per table. */
const sizeKey = (id: string) => `opennvr.pageSize.${id}`

function readStoredSize(id: string | undefined, fallback: number): number {
  if (!id) return fallback
  try {
    const raw = window.localStorage.getItem(sizeKey(id))
    const n = raw ? Number(raw) : NaN
    return Number.isFinite(n) && n > 0 ? n : fallback
  } catch {
    // Private modes throw on access rather than returning null.
    return fallback
  }
}

/**
 * Page state for a server-paged table: 1-based `page`, a `pageSize` the
 * operator can change, and the `skip` the API wants.
 *
 * `storageId` persists the chosen size for that table. Worth it because
 * these tables used to show up to 200 rows in one scroll; someone who
 * prefers 100 should say so once, not on every visit.
 *
 * Filters deliberately do NOT reset the page from inside here. Every
 * caller resets explicitly in its own change handler, which is how the
 * app's existing paginated views do it — an effect keyed on the filters
 * would also fire on mount and would fight any future restore of page
 * state from the URL.
 */
export function usePagination(initialPageSize = 25, storageId?: string) {
  const [page, setPage] = useState(1)
  const [pageSize, setPageSizeState] = useState(() =>
    readStoredSize(storageId, initialPageSize))

  const setPageSize = useCallback((size: number) => {
    setPageSizeState(size)
    // Row 1 of the new size, not the row that happened to be first
    // before: the app's other paginated views behave this way, and a
    // consistent surprise beats a clever one.
    setPage(1)
    if (storageId) {
      try {
        window.localStorage.setItem(sizeKey(storageId), String(size))
      } catch {
        // Not being able to remember a preference is not an error.
      }
    }
  }, [storageId])

  const reset = useCallback(() => setPage(1), [])
  const skip = useMemo(() => (page - 1) * pageSize, [page, pageSize])

  return { page, pageSize, skip, setPage, setPageSize, reset }
}
