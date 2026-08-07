import { useEffect, type RefObject } from 'react'

// Closes floating menus/dropdowns when the user clicks outside `ref`.
// Listens only while `active` is true so idle components add no listeners.
export function useClickOutside(ref: RefObject<HTMLElement | null>, active: boolean, onOutside: () => void) {
  useEffect(() => {
    if (!active) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onOutside()
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [ref, active, onOutside])
}
