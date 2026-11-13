import { useCallback, useEffect, useMemo, useState } from 'react'

/**
 * Checkbox selection for a paged table, mail-client style.
 *
 * Two distinct states, and conflating them is the classic bug here:
 *
 *  - `selected` — specific rows the operator ticked, by id.
 *  - `allMatching` — "everything the current filter matches", including
 *    rows on pages nobody has loaded. It cannot be a set of ids because
 *    those ids are not in the browser; it has to travel to the server as
 *    the FILTER, which is why the ack endpoint takes one.
 *
 * Selection resets whenever the view changes. Carrying ticks across a
 * filter change would let a bulk action hit rows the operator can no
 * longer see, which is exactly the wrong surprise for a control that
 * silences alarms.
 */
export function useRowSelection<T extends string | number>(resetKey: unknown) {
  const [selected, setSelected] = useState<Set<T>>(() => new Set())
  const [allMatching, setAllMatching] = useState(false)

  const clear = useCallback(() => {
    setSelected(new Set())
    setAllMatching(false)
  }, [])

  // The key is a serialised view (filters + page), so any change to what
  // is on screen drops the selection.
  const key = useMemo(() => JSON.stringify(resetKey), [resetKey])
  useEffect(() => { clear() }, [key, clear])

  const toggle = useCallback((id: T) => {
    setAllMatching(false)
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  /** Tick or untick every id on the current page. */
  const toggleMany = useCallback((ids: T[], on: boolean) => {
    setAllMatching(false)
    setSelected((prev) => {
      const next = new Set(prev)
      ids.forEach((id) => (on ? next.add(id) : next.delete(id)))
      return next
    })
  }, [])

  return {
    selected,
    allMatching,
    /** Escalate from "this page" to "everything the filter matches". */
    selectAllMatching: () => setAllMatching(true),
    toggle,
    toggleMany,
    clear,
    has: (id: T) => selected.has(id),
    count: selected.size,
  }
}
