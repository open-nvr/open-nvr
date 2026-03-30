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

type Props = {
  whepUrl: string
  muted?: boolean
  playsInline?: boolean
  className?: string
}

export type PlayerHandle = {
  play: () => Promise<void>
  pause: () => void
  snapshot: () => string | null
  requestFullscreen: () => void
  getElement: () => HTMLVideoElement | null
}

export const WebRtcWhepPlayer = forwardRef<PlayerHandle, Props>(function WebRtcWhepPlayer({ whepUrl, muted = true, playsInline = true, className }: Props, ref) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const pcRef = useRef<RTCPeerConnection | null>(null)
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
    let aborted = false
    let resourceUrl: string | null = null
    const controller = new AbortController()

    async function start() {
      try {
        setError(null)
        const pc = new RTCPeerConnection({
          iceServers: [{ urls: ['stun:stun.l.google.com:19302'] }],
        })
        pcRef.current = pc
        pc.addTransceiver('video', { direction: 'recvonly' })
        pc.addTransceiver('audio', { direction: 'recvonly' })
        pc.ontrack = (ev) => {
          const [stream] = ev.streams
          const el = videoRef.current
          if (el && stream) {
            el.srcObject = stream
            el.play().catch(() => {})
          }
        }

        const offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        const resp = await fetch(whepUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/sdp' },
          body: offer.sdp || '',
          signal: controller.signal,
        })
        if (!(resp.status === 200 || resp.status === 201)) {
          throw new Error(`WHEP POST failed: ${resp.status}`)
        }
        resourceUrl = resp.headers.get('Location') || null
        const answerSdp = await resp.text()
        await pc.setRemoteDescription({ type: 'answer', sdp: answerSdp })
      } catch (e: any) {
        if (!aborted) setError(e?.message || 'WebRTC setup error')
      }
    }

    start()

    return () => {
      aborted = true
      controller.abort()
      try {
        if (pcRef.current) {
          pcRef.current.getSenders().forEach(s => { try { s.track && s.track.stop() } catch {} })
          pcRef.current.close()
        }
      } catch {}
      pcRef.current = null
      if (resourceUrl) {
        // Try to DELETE the WHEP resource
        fetch(resourceUrl, { method: 'DELETE' }).catch(() => {})
      }
    }
  }, [whepUrl])

  return (
    <div className={className}>
      <video ref={videoRef} autoPlay muted={muted} playsInline={playsInline} className="w-full h-full object-contain bg-black" />
      {error && <div className="absolute left-2 bottom-2 text-xs bg-black/70 px-1 py-0.5 text-red-400">{error}</div>}
    </div>
  )
})


