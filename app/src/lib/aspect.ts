// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Display-aspect correction for anamorphic camera streams (issue #354).
 *
 * Some encoders deliberately squash a dimension and expect the player to
 * stretch it back. Dahua-family DVRs (CP Plus "1080N") encode a 16:9 scene
 * as 960x1080 — half the horizontal resolution — and their own client
 * hardcodes the 2x stretch. The bitstream carries NO sample aspect ratio in
 * the SPS VUI, so a standards-compliant player has nothing to correct from
 * and renders a narrow near-portrait strip.
 *
 * A camera that DOES declare a correct SAR is a related but separate case:
 * it renders fine over HLS and still squished over WebRTC, because Chrome's
 * WebRTC pipeline reports the coded size and drops the VUI aspect ratio.
 * Nothing in the browser exposes that difference — catching it needs the
 * backend to measure the source and hand the answer down, which is not wired
 * up yet. The operator override covers those cameras in the meantime.
 *
 * Resolution order, most specific first:
 *   1. operator override on the camera row  ('native' | 'W:H')
 *   2. known anamorphic mode table          (encoders that declare nothing)
 *   3. the coded aspect                     (every normal camera, untouched)
 */

/**
 * Per-camera operator override. 'auto' (or null/undefined) runs the
 * resolution order above; 'native' pins the coded aspect and disables the
 * correction entirely — the escape hatch for a camera the table gets wrong.
 * Anything else is a `width:height` ratio.
 */
export type AspectOverride = 'auto' | 'native' | (string & {})

/**
 * Encoder modes that squash a dimension and signal no SAR/DAR at all.
 *
 * Matched on the EXACT coded size, never on a ratio: a genuinely portrait
 * corridor-mode camera (1080x1920) or a 1:1 fisheye must be left alone, and
 * a ratio test would "correct" both into garbage.
 */
const ANAMORPHIC: ReadonlyArray<{ w: number; h: number; dar: number }> = [
  { w: 960, h: 1080, dar: 16 / 9 }, // Dahua / CP Plus "1080N"
  { w: 640, h: 720, dar: 16 / 9 }, // Dahua "720N"
  { w: 960, h: 576, dar: 4 / 3 }, // "960H" PAL
  { w: 960, h: 480, dar: 4 / 3 }, // "960H" NTSC
  { w: 704, h: 576, dar: 4 / 3 }, // D1 PAL
  { w: 704, h: 480, dar: 4 / 3 }, // D1 NTSC
]

/**
 * Parse a display aspect ratio. Accepts "16:9", "4/3", or a bare decimal
 * ("1.777"), which is what ffprobe and the settings field between them can
 * produce. Returns null for anything unusable, so a bad stored value degrades
 * to "no correction" rather than a NaN layout.
 */
export function parseRatio(value: string | number | null | undefined): number | null {
  if (typeof value === 'number') return Number.isFinite(value) && value > 0 ? value : null
  if (!value) return null
  const text = value.trim()
  const pair = text.match(/^(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)$/)
  if (pair) {
    const w = Number(pair[1])
    const h = Number(pair[2])
    return h > 0 && w > 0 ? w / h : null
  }
  const scalar = Number(text)
  return Number.isFinite(scalar) && scalar > 0 ? scalar : null
}

/**
 * The aspect ratio (width/height) the frame should be RENDERED at.
 *
 * Returns null while the coded size is unknown — callers keep their
 * "no metadata yet" layout in that case rather than guessing.
 */
export function displayAspect(
  codedWidth: number | null | undefined,
  codedHeight: number | null | undefined,
  override?: AspectOverride | null,
): number | null {
  if (!codedWidth || !codedHeight) return null
  const native = codedWidth / codedHeight

  if (override && override !== 'auto') {
    if (override === 'native') return native
    return parseRatio(override) ?? native
  }

  const known = ANAMORPHIC.find((m) => m.w === codedWidth && m.h === codedHeight)
  return known ? known.dar : native
}

/**
 * True when the frame must be STRETCHED (object-fit: fill) rather than
 * letterboxed — i.e. the display aspect differs from the coded one.
 *
 * The 1% tolerance keeps float noise from flipping an ordinary camera into
 * fill mode, where a rounding error would show as a barely-visible smear.
 */
export function isStretched(
  codedWidth: number | null | undefined,
  codedHeight: number | null | undefined,
  effective: number | null,
): boolean {
  if (!codedWidth || !codedHeight || !effective) return false
  return Math.abs(codedWidth / codedHeight - effective) / effective > 0.01
}

/**
 * Canvas size for a snapshot or export of an anamorphic frame: keep every
 * coded scanline and widen to the display aspect, so the saved image matches
 * the tile it was taken from. 960x1080 at 16:9 -> 1920x1080.
 */
export function snapshotSize(
  codedWidth: number,
  codedHeight: number,
  effective: number | null,
): { width: number; height: number } {
  if (!isStretched(codedWidth, codedHeight, effective)) {
    return { width: codedWidth, height: codedHeight }
  }
  return { width: Math.max(1, Math.round(codedHeight * effective!)), height: codedHeight }
}

/** Presets for the per-camera setting; 'custom' reveals a free-form W:H field. */
export const ASPECT_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'auto', label: 'Auto (detect)' },
  { value: 'native', label: 'Native (as encoded)' },
  { value: '16:9', label: '16:9 (widescreen)' },
  { value: '4:3', label: '4:3 (standard)' },
  { value: 'custom', label: 'Custom…' },
]

/** True when `value` needs the free-form field rather than a preset. */
export function isCustomAspect(value: string | null | undefined): boolean {
  if (!value || value === 'auto' || value === 'native') return false
  return value !== '16:9' && value !== '4:3'
}
