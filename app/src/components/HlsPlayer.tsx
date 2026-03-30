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

import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import Hls from 'hls.js'

type Props = {
  hlsUrl: string
  muted?: boolean
  autoPlay?: boolean
  playsInline?: boolean
  className?: string
}

export type HlsHandle = {
  play: () => Promise<void>
  pause: () => void
  snapshot: () => string | null
  requestFullscreen: () => void
  getElement: () => HTMLVideoElement | null
}

export const HlsPlayer = forwardRef<HlsHandle, Props>(function HlsPlayer({ hlsUrl, muted = true, autoPlay = true, playsInline = true, className }: Props, ref) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const hlsRef = useRef<Hls | null>(null)
  const [error, setError] = useState<string | null>(null)

  useImperativeHandle(ref, () => ({
    play: async () => {
      const el = videoRef.current
      if (el) await el.play().catch(() => {})
    },
    pause: () => {
      const el = videoRef.current
      if (el) el.pause()
    },
    snapshot: () => {
      const el = videoRef.current
      if (!el || el.readyState < 2) return null
      const w = el.videoWidth || el.clientWidth
      const h = el.videoHeight || el.clientHeight
      if (!w || !h) return null
      const canvas = document.createElement('canvas')
      canvas.width = w; canvas.height = h
      const ctx = canvas.getContext('2d')
      if (!ctx) return null
      try { ctx.drawImage(el, 0, 0, w, h) } catch { return null }
      return canvas.toDataURL('image/jpeg', 0.92)
    },
    requestFullscreen: () => {
      const el = videoRef.current
      if (!el) return
      const fn = (el as any).requestFullscreen || (el as any).webkitRequestFullscreen || (el as any).msRequestFullscreen
      if (fn) fn.call(el)
    },
    getElement: () => videoRef.current,
  }), [])

  useEffect(() => {
    const el = videoRef.current
    if (!el || !hlsUrl) return

    setError(null)
    el.muted = muted
    el.playsInline = playsInline
    el.autoplay = autoPlay

    // Check if native HLS is supported (Safari)
    if (el.canPlayType('application/vnd.apple.mpegurl')) {
      el.src = hlsUrl
      el.play().catch((e) => setError(e?.message || 'Failed to play HLS'))
      return
    }

    // Use HLS.js for other browsers
    if (Hls.isSupported()) {
      // Clean up previous HLS instance
      if (hlsRef.current) {
        hlsRef.current.destroy()
        hlsRef.current = null
      }

      const hls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 90,
        maxBufferLength: 30,
        maxMaxBufferLength: 60,
        startLevel: -1, // Auto-select quality
        debug: false,
      })

      hlsRef.current = hls

      hls.loadSource(hlsUrl)
      hls.attachMedia(el)

      hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
        console.log('[HLS] Manifest parsed, levels:', data.levels?.length)
        if (autoPlay) {
          el.play().catch((e) => {
            console.warn('[HLS] Autoplay blocked:', e?.message)
            // Try muted autoplay as fallback
            el.muted = true
            el.play().catch(() => {
              setError('Autoplay blocked - click to play')
            })
          })
        }
      })

      hls.on(Hls.Events.FRAG_LOADED, () => {
        // Fragment loaded successfully - video should start soon
        if (el.paused && autoPlay) {
          el.play().catch(() => {})
        }
      })

      hls.on(Hls.Events.ERROR, (event, data) => {
        console.error('[HLS] Error:', data.type, data.details, data)
        if (data.fatal) {
          switch (data.type) {
            case Hls.ErrorTypes.NETWORK_ERROR:
              setError('Network error: Failed to load HLS stream')
              // Try to recover
              setTimeout(() => hls.startLoad(), 1000)
              break
            case Hls.ErrorTypes.MEDIA_ERROR:
              setError('Media error: Failed to decode video')
              hls.recoverMediaError()
              break
            default:
              setError(`Fatal error: ${data.type} - ${data.details}`)
              hls.destroy()
              break
          }
        } else {
          // Non-fatal error, log but don't show to user
          console.warn('[HLS] Non-fatal error:', data.details)
        }
      })
    } else {
      setError('HLS is not supported in this browser')
    }

    // Cleanup
    return () => {
      if (hlsRef.current) {
        hlsRef.current.destroy()
        hlsRef.current = null
      }
    }
  }, [hlsUrl, muted, autoPlay, playsInline])

  return (
    <div className={className}>
      <video ref={videoRef} controls autoPlay={autoPlay} muted={muted} playsInline={playsInline} className="w-full h-full object-contain bg-black" />
      {error && <div className="absolute left-2 bottom-2 text-xs bg-black/70 px-1 py-0.5 text-red-400">{error}</div>}
    </div>
  )
})


