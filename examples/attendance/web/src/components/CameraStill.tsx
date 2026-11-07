// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What a camera sees right now, as one still: so a camera is picked by
// its view, not only by its name.

import { useEffect, useState } from 'react'
import { CameraOff, RefreshCw } from 'lucide-react'
import { api } from '../attendance'
import { Spinner } from './ui'

/** A still takes core a capture from the camera; a few at a time, so a
 * site with many cameras does not ask for all of them at once. */
const AT_ONCE = 3
let running = 0
const waiting: (() => void)[] = []

async function queued<T>(job: () => Promise<T>): Promise<T> {
  if (running >= AT_ONCE) await new Promise<void>((go) => waiting.push(go))
  running++
  try {
    return await job()
  } finally {
    running--
    waiting.shift()?.()
  }
}

export function CameraStill({ id, className = '' }: { id: number; className?: string }) {
  const [url, setUrl] = useState<string | null>(null)
  const [state, setState] = useState<'loading' | 'ok' | 'none'>('loading')
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let live = true
    let made: string | null = null
    setState('loading')
    queued(() => api.cameraStill(id))
      .then((blob) => {
        if (!live) return
        if (!blob) return setState('none')
        made = URL.createObjectURL(blob)
        setUrl(made)
        setState('ok')
      })
      .catch(() => live && setState('none'))
    return () => {
      live = false
      if (made) URL.revokeObjectURL(made)
    }
  }, [id, tick])

  return (
    <div className={`relative aspect-video overflow-hidden rounded-md bg-gray-900 ${className}`}>
      {state === 'ok' && url && <img src={url} alt="" className="h-full w-full object-cover" />}
      {state === 'loading' && (
        <div className="absolute inset-0 flex items-center justify-center text-gray-400">
          <Spinner />
        </div>
      )}
      {state === 'none' && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 text-xs text-gray-400">
          <CameraOff className="h-5 w-5" />
          No picture (camera offline?)
        </div>
      )}
      {state !== 'loading' && (
        <button
          type="button"
          onClick={(e) => {
            e.preventDefault()
            e.stopPropagation()
            setTick((t) => t + 1)
          }}
          className="absolute right-1 top-1 rounded bg-black/50 p-1 text-white hover:bg-black/70"
          title="Refresh the picture"
          aria-label="Refresh the picture"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  )
}
