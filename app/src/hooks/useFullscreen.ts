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

import { RefObject, useCallback, useEffect, useState } from 'react'

export function useFullscreen<T extends HTMLElement>(ref: RefObject<T>) {
  const [isFullscreen, setIsFullscreen] = useState(false)

  const getFsElement = () =>
    (document.fullscreenElement ||
      (document as any).webkitFullscreenElement ||
      (document as any).msFullscreenElement) as Element | null

  const enter = useCallback(async () => {
    const target = ref.current
    if (!target) return
    try {
      const el = target as any
      if (el.requestFullscreen) await el.requestFullscreen()
      else if (el.webkitRequestFullscreen) await el.webkitRequestFullscreen()
      else if (el.msRequestFullscreen) await el.msRequestFullscreen()
    } catch {
      // ignore
    }
  }, [ref])

  const exit = useCallback(async () => {
    try {
      const doc: any = document
      if (document.exitFullscreen) await document.exitFullscreen()
      else if (doc.webkitExitFullscreen) await doc.webkitExitFullscreen()
      else if (doc.msExitFullscreen) await doc.msExitFullscreen()
    } catch {
      // ignore
    }
  }, [])

  const toggle = useCallback(() => {
    if (getFsElement()) exit()
    else enter()
  }, [enter, exit])

  useEffect(() => {
    const handler = () => setIsFullscreen(!!getFsElement())
    document.addEventListener('fullscreenchange', handler)
    document.addEventListener('webkitfullscreenchange', handler as any)
    document.addEventListener('msfullscreenchange', handler as any)
    handler()
    return () => {
      document.removeEventListener('fullscreenchange', handler)
      document.removeEventListener('webkitfullscreenchange', handler as any)
      document.removeEventListener('msfullscreenchange', handler as any)
    }
  }, [])

  return { isFullscreen, enter, exit, toggle }
}
