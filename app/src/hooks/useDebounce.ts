import { useEffect, useState } from 'react'

/**
 * A value that settles `delay` ms after the input stops changing.
 *
 * Extracted from two identical inline copies (the Vehicles plate search
 * and the Cameras search box). The reason it exists is worth keeping
 * with it: a search box wired straight into a react-query key swaps that
 * key on every keystroke, and since these tables do not blank-guard
 * their results, a six-character plate meant six rounds of the table
 * emptying and every row thumbnail remounting.
 *
 * 250 ms is the value both call sites already used.
 */
export function useDebounce<T>(value: T, delay = 250): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setSettled(value), delay)
    return () => clearTimeout(t)
  }, [value, delay])
  return settled
}
