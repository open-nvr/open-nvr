// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CameraOff } from 'lucide-react'
import { clsx } from 'clsx'
import { apiService } from '../../lib/apiService'

/**
 * A camera's still for a picker (GET /tours/thumbnails/:id). Loads only once
 * it scrolls into view, so a list of 1000 cameras asks for the dozen on
 * screen. A grab waits for the camera's next keyframe, so the first look
 * costs seconds; the server and the browser then keep the still five minutes.
 */
export function CameraThumb({ cameraId, className, alt }: { cameraId: number; className?: string; alt: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const [seen, setSeen] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el || seen) return
    if (typeof IntersectionObserver === 'undefined') { setSeen(true); return }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { setSeen(true); io.disconnect() }
    }, { rootMargin: '120px' })
    io.observe(el)
    return () => io.disconnect()
  }, [seen])

  const still = useQuery({
    queryKey: ['camera-thumb', cameraId],
    enabled: seen,
    queryFn: async () => (await apiService.getCameraThumbnail(cameraId)).data as Blob,
    staleTime: 5 * 60_000,
    gcTime: 10 * 60_000,
    // One more try a little later: right after a restart, or while the grab
    // queue is long, the first grab of a live camera can time out.
    retry: 1,
    retryDelay: 3_000,
  })
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!(still.data instanceof Blob)) { setUrl(null); return }
    const u = URL.createObjectURL(still.data)
    setUrl(u)
    return () => URL.revokeObjectURL(u)
  }, [still.data])

  return (
    // No position class here: callers place it (`absolute inset-0` in a
    // preview tile), and a base `relative` would win over theirs in the
    // stylesheet, leaving the still at its natural height, spilling out.
    <div ref={ref} className={clsx('overflow-hidden bg-[#0b0f16] flex items-center justify-center', className)}>
      {url ? (
        <img src={url} alt={alt} className="block w-full h-full object-cover" draggable={false} />
      ) : still.isError ? (
        <CameraOff size={14} className="text-white/35" aria-label={alt} />
      ) : (
        <div className="w-full h-full animate-pulse bg-white/5" aria-hidden="true" />
      )}
    </div>
  )
}
