// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What a viewer can switch on and off in a live overlay: its "layers".
//
// An app box may name its layer (overlay.boxes.v1 `layer`). When it does
// not, layers are read from the labels themselves:
// the detector's boxes by class ("person", "car"), an app's boxes by what
// the label says once its live numbers are set aside. "Bar #23" and
// "Bar #72" are one layer, "Trays: 24" and "Trays: 25" are one layer, and
// "24 of 27 in view · all accounted for" stays one layer whatever the
// counts or the verdict after the "·".

// No imports, so node's test runner can load this file as it is
// (tests/overlayLayers.test.mjs). Same value as overlayMerge's.
const TIER0_SOURCE = 'tier0'

/** The pseudo-layer for the detector's object-count / fps corner badge. */
export const BADGE_LAYER = 'badge'

export interface OverlayLayer {
  key: string
  /** What the viewer reads in the list. */
  name: string
  /** "Detector" or the app's id, for grouping the list. */
  group: string
}

/** The part of a label that names its kind: before any "·", ":" or "#",
 *  with numbers dropped. */
function kindOf(label: string): string {
  return label.split(/[·:#]/)[0].replace(/\d+(\.\d+)?%?/g, ' ').replace(/\s+/g, ' ').trim()
}

export function layerOf(source: string, label: string, layer?: string | null): OverlayLayer {
  // The app said which layer: that wins over anything read from labels.
  if (layer && source !== TIER0_SOURCE) {
    return { key: `${source}|layer|${layer}`, name: layer, group: source.replace(/^app:/, '') }
  }
  if (source === TIER0_SOURCE) {
    const cls = label.replace(/_/g, ' ')
    return { key: `${TIER0_SOURCE}|${label}`, name: cls.charAt(0).toUpperCase() + cls.slice(1), group: 'Detector' }
  }
  const kind = kindOf(label) || label
  // The name keeps the numbers ("24 of 27 in view") so the list reads
  // like what is on screen; the key does not, so it stays one layer.
  const name = label.split(/[·:]/)[0].trim() || label
  return { key: `${source}|${kind}`, name, group: source.replace(/^app:/, '') }
}

/** Remembered per camera, in this browser. */
export function hiddenLayersKey(cameraId: number): string {
  return `opennvr.overlay.hidden.${cameraId}`
}
