// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useEffect, useState } from 'react'
import type { RefObject } from 'react'

export interface VideoSize {
  width: number
  height: number
}

/**
 * Coded frame size of the media in `ref`, tracked across source changes.
 *
 * 'loadedmetadata' is the first point the size is known; 'resize' fires when
 * the decoded size changes mid-stream (an HLS level switch, or a camera
 * re-negotiating); 'emptied' clears it when the source is torn down, so a
 * stale aspect never outlives the stream that produced it.
 *
 * Note this is the CODED size — for an anamorphic stream it is NOT the size
 * the frame should be displayed at. Feed it through `displayAspect()`.
 */
export function useVideoSize(ref: RefObject<HTMLVideoElement | null>): VideoSize | null {
  const [size, setSize] = useState<VideoSize | null>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const update = () => {
      setSize(
        el.videoWidth && el.videoHeight
          ? { width: el.videoWidth, height: el.videoHeight }
          : null
      )
    }
    update()
    el.addEventListener('loadedmetadata', update)
    el.addEventListener('resize', update)
    el.addEventListener('emptied', update)
    return () => {
      el.removeEventListener('loadedmetadata', update)
      el.removeEventListener('resize', update)
      el.removeEventListener('emptied', update)
    }
  }, [ref])

  return size
}
