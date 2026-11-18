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

/**
 * What a digitally zoomed picture shows over itself: the zoom level with
 * − / + / reset while zoomed, and, after a plain scroll over the picture,
 * a brief reminder that Ctrl + scroll zooms.
 */

import { Minus, Plus, RotateCcw } from 'lucide-react'
import { useTranslation } from '../i18n'
import type { useDigitalZoom } from '../hooks/useDigitalZoom'
import { MAX_ZOOM } from '../hooks/useDigitalZoom'

export function ZoomBadge({ zoom, className = 'top-8 right-2' }: {
  zoom: ReturnType<typeof useDigitalZoom>
  /** Where in the surface it sits (Tailwind position classes). */
  className?: string
}) {
  const { t } = useTranslation()
  const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)
  return (
    <>
      {zoom.zoomed && (
        <div
          data-no-zoom-pan
          className={`absolute ${className} z-20 flex items-center gap-px bg-black/70 border border-white/20 text-white text-[11px] font-mono`}
          onDoubleClick={(e) => e.stopPropagation()}
        >
          <button type="button" className="p-1 hover:bg-white/15" title={t('zoom.out')} aria-label={t('zoom.out')}
            onClick={(e) => { e.stopPropagation(); zoom.zoomCentre(1 / 1.5) }}>
            <Minus size={12} />
          </button>
          <span className="px-1 tabular-nums" aria-live="polite">{zoom.scale.toFixed(1)}×</span>
          <button type="button" className="p-1 hover:bg-white/15 disabled:opacity-40" title={t('zoom.in')} aria-label={t('zoom.in')}
            disabled={zoom.scale >= MAX_ZOOM}
            onClick={(e) => { e.stopPropagation(); zoom.zoomCentre(1.5) }}>
            <Plus size={12} />
          </button>
          <button type="button" className="p-1 hover:bg-white/15" title={t('zoom.reset')} aria-label={t('zoom.reset')}
            onClick={(e) => { e.stopPropagation(); zoom.reset() }}>
            <RotateCcw size={12} />
          </button>
        </div>
      )}
      {zoom.hint && !zoom.zoomed && (
        <div className="absolute inset-x-0 top-1/2 -translate-y-1/2 z-20 flex justify-center pointer-events-none">
          <span className="px-2 py-1 bg-black/75 text-white text-xs">
            {t(isMac ? 'zoom.hintMac' : 'zoom.hint')}
          </span>
        </div>
      )}
    </>
  )
}
