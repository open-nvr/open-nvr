/**
 * Copyright (c) 2026 OpenNVR
 * This file is part of OpenNVR.
 *
 * OpenNVR is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * OpenNVR is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.
 */

/** The PTZ pad's keyboard and preset rules, pure so they can be tested. */

/** Pan, tilt, zoom direction, each -1, 0 or 1; scaled by the speed. */
export type Vec = readonly [number, number, number]

export type Preset = { token: string; name: string }

/** Direction keys by ``KeyboardEvent.code``, so Shift (fast) doesn't turn
 *  "-" into "_" or "=" into "+". */
export const DIR_KEYS: Record<string, Vec> = {
  ArrowUp: [0, 1, 0],
  ArrowDown: [0, -1, 0],
  ArrowLeft: [-1, 0, 0],
  ArrowRight: [1, 0, 0],
  Equal: [0, 0, 1],
  NumpadAdd: [0, 0, 1],
  Minus: [0, 0, -1],
  NumpadSubtract: [0, 0, -1],
}

/** The direction of every held key together: ↑ and → is up-right. Opposite
 *  keys cancel. */
export function heldVector(held: Iterable<string>): Vec {
  let x = 0
  let y = 0
  let z = 0
  for (const code of held) {
    const v = DIR_KEYS[code]
    if (!v) continue
    x += v[0]
    y += v[1]
    z += v[2]
  }
  const c = (n: number) => Math.max(-1, Math.min(1, n))
  return [c(x), c(y), c(z)]
}

/** The pad button a key vector lights up ('ur', 'zi', …), or null. */
export function buttonFor(v: Vec): string | null {
  if (v[2] > 0) return 'zi'
  if (v[2] < 0) return 'zo'
  const id = (v[1] > 0 ? 'u' : v[1] < 0 ? 'd' : '') + (v[0] < 0 ? 'l' : v[0] > 0 ? 'r' : '')
  return id || null
}

/** 1–9 from Digit1…Digit9 or Numpad1…Numpad9, else null. */
export function digitOf(code: string): number | null {
  const m = /^(?:Digit|Numpad)([1-9])$/.exec(code)
  return m ? Number(m[1]) : null
}

/** Preset ``n`` as the camera numbers it (token "3", or a name such as
 *  "3" / "Preset 3"), else the n-th preset in the camera's list. */
export function presetForDigit(presets: Preset[], n: number): Preset | undefined {
  const s = String(n)
  const named = new RegExp(`^(preset[\\s_-]*)?0*${s}$`, 'i')
  return (
    presets.find((p) => p.token === s) ??
    presets.find((p) => named.test(p.name.trim())) ??
    presets[n - 1]
  )
}

/** The preset named "Home" (any case), if the camera has one. */
export function homePreset(presets: Preset[]): Preset | undefined {
  return presets.find((p) => /^home$/i.test(p.name.trim()))
}
