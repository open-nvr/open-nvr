// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import type { ReactNode } from 'react'

/**
 * A box of the given DISPLAY aspect, centred in its parent and never
 * overflowing it — the frame an anamorphic stream gets stretched into.
 *
 * The parent must be a size container (`containerType: 'size'`) with a
 * definite height: `100cqw` is the parent's width, so
 * `height = min(100%, 100cqw / aspect)` is the tallest the box can be while
 * still fitting horizontally, and `aspect-ratio` then derives the width.
 *
 * `aspect` is null until the stream's metadata lands, in which case the box
 * simply fills the parent and the video inside stays on object-contain.
 */
export function AspectFrame({
  aspect,
  className = '',
  children,
}: {
  aspect: number | null
  className?: string
  children: ReactNode
}) {
  return (
    <div
      className={`relative ${className}`}
      style={
        aspect
          ? { height: `min(100%, calc(100cqw / ${aspect}))`, aspectRatio: String(aspect) }
          : { width: '100%', height: '100%' }
      }
    >
      {children}
    </div>
  )
}
