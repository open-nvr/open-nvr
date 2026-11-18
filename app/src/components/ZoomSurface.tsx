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

import type { ReactNode } from 'react'
import { useDigitalZoom } from '../hooks/useDigitalZoom'
import { ZoomBadge } from './ZoomBadge'

/** Fills its parent with a digitally zoomable picture (see useDigitalZoom):
 *  for the playback players, whose <video> sits straight in an AspectFrame.
 *  VideoPlayer wires the hook itself, to keep its detection overlay crisp. */
export function ZoomSurface({ children, badgeClassName = 'bottom-2 right-2' }: {
  children: ReactNode
  badgeClassName?: string
}) {
  const zoom = useDigitalZoom()
  return (
    <div {...zoom.surfaceProps} className="relative w-full h-full overflow-hidden">
      <div className="w-full h-full" style={zoom.layerStyle}>
        {children}
      </div>
      <ZoomBadge zoom={zoom} className={badgeClassName} />
    </div>
  )
}
