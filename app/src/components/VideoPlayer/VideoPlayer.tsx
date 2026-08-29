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

import {
  forwardRef,
  useRef,
  useEffect,
  useState,
  useImperativeHandle,
  useCallback,
  useMemo,
} from 'react'
import { apiService } from '../../lib/apiService'
import type Hls from 'hls.js'
import { loadHls } from '../../lib/loadHls'
import { displayAspect, isStretched, snapshotSize } from '../../lib/aspect'
import type { AspectOverride } from '../../lib/aspect'
import { useVideoSize } from '../../hooks/useVideoAspect'
import { VideoControls } from './VideoControls'
import { AlertCircle } from 'lucide-react'

export type VideoPlayerMode = 'live' | 'playback'
export type StreamType = 'webrtc' | 'hls' | 'mp4'

export interface VideoPlayerProps {
  /** Mode: 'live' for live streaming, 'playback' for recorded videos */
  mode: VideoPlayerMode
  /** WebRTC WHEP URL (live mode) */
  whepUrl?: string
  /** HLS URL (live mode) */
  hlsUrl?: string
  /** MP4 URL (playback mode - direct MediaMTX URL) */
  mp4Url?: string
  /** HLS VOD URL (playback mode - backend-generated manifest) */
  hlsPlaybackUrl?: string
  /** MediaMTX JWT token for stream authentication */
  mediamtxToken?: string
  /** Preferred stream type for live mode */
  preferredStreamType?: 'webrtc' | 'hls'
  /** Preferred playback type for playback mode */
  preferredPlaybackType?: 'hls' | 'mp4'
  /** Camera/stream name overlay */
  title?: string
  /** Auto play on load */
  autoPlay?: boolean
  /** Start muted */
  muted?: boolean
  /** CSS class */
  className?: string
  /** Callback when snapshot is taken */
  onSnapshot?: (dataUrl: string) => void
  /** Callback on error */
  onError?: (error: string) => void
  /** Live mode: a stream request was rejected as unauthorized — the
      mediamtxToken has expired. The parent owns the token, so it must
      refetch stream URLs; the changed token prop re-runs setup here. */
  onAuthExpired?: () => void
  /** Callback when HLS playback fails (to trigger fallback) */
  onHlsPlaybackError?: () => void
  /** Present only when the camera supports PTZ; toggles the PTZ pad */
  onTogglePtz?: () => void
  ptzActive?: boolean
  /** Extra overlay rendered inside the player (e.g. the PTZ pad) — kept
      inside so it stays visible when the player element goes fullscreen */
  overlay?: React.ReactNode
  /** Operator's per-camera display-aspect override ('auto' | 'native' | 'W:H').
      Absent or 'auto' runs the detection in lib/aspect.ts (issue #354). */
  displayAspectOverride?: AspectOverride | null
}

export interface VideoPlayerHandle {
  play: () => Promise<void>
  pause: () => void
  snapshot: () => string | null
  requestFullscreen: () => void
  getVideoElement: () => HTMLVideoElement | null
  switchStreamType: (type: 'webrtc' | 'hls') => void
}

export const VideoPlayer = forwardRef<VideoPlayerHandle, VideoPlayerProps>(
  function VideoPlayer(
    {
      mode,
      whepUrl,
      hlsUrl,
      mp4Url,
      hlsPlaybackUrl,
      mediamtxToken,
      preferredStreamType = 'webrtc',
      preferredPlaybackType = 'hls',
      title,
      autoPlay = true,
      muted = true,
      className = '',
      onSnapshot,
      onError,
      onAuthExpired,
      onHlsPlaybackError,
      onTogglePtz,
      ptzActive = false,
      overlay,
      displayAspectOverride,
    },
    ref
  ) {
    const containerRef = useRef<HTMLDivElement>(null)
    const videoRef = useRef<HTMLVideoElement>(null)
    const hlsInstanceRef = useRef<Hls | null>(null)
    const pcRef = useRef<RTCPeerConnection | null>(null)
    const whepResourceRef = useRef<string | null>(null)

    // Determine initial stream type based on mode and available URLs
    const getInitialStreamType = (): StreamType => {
      if (mode === 'playback') {
        // Prefer HLS for playback if available, fallback to MP4
        if (hlsPlaybackUrl && preferredPlaybackType === 'hls') return 'hls'
        return 'mp4'
      }
      return preferredStreamType
    }

    const [streamType, setStreamType] = useState<StreamType>(getInitialStreamType())
    const [isPlaying, setIsPlaying] = useState(false)
    const [isMuted, setIsMuted] = useState(muted)
    const [volume, setVolume] = useState(muted ? 0 : 1)
    const [currentTime, setCurrentTime] = useState(0)
    const [duration, setDuration] = useState(0)
    const [buffered, setBuffered] = useState(0)
    const [isFullscreen, setIsFullscreen] = useState(false)
    const [isLoading, setIsLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)
    const [showControls, setShowControls] = useState(true)
    const hideControlsTimeout = useRef<number | null>(null)
    // Bounded auto-reconnect (live mode). Covers stream drops the backend
    // can't push an event for (e.g. a network blip between this browser and
    // MediaMTX). Refs, not state, so the setup callbacks' event handlers can
    // use them without creating dependency cycles.
    const retryCountRef = useRef(0)
    const retryTimerRef = useRef<number | null>(null)
    const disconnectGraceRef = useRef<number | null>(null)
    const restartRef = useRef<() => void>(() => {})
    // Slow, unbounded poll while the WHEP path 404s (path missing
    // server-side, e.g. a MediaMTX restart raced the backend's
    // re-provisioning). Separate from the bounded fast-retry budget: the
    // offline overlay stays up while probe attempts run.
    const offlinePollTimerRef = useRef<number | null>(null)
    const offlinePollingRef = useRef(false)
    // Via a ref so the setup callbacks don't take the parent's callback
    // identity as a dependency — an unstable identity would tear down and
    // rebuild the stream on every parent render.
    const onAuthExpiredRef = useRef(onAuthExpired)
    onAuthExpiredRef.current = onAuthExpired
    const [isReconnecting, setIsReconnecting] = useState(false)
    // Coded frame size of the incoming stream, read from the video metadata.
    // null until known (or after the source is torn down).
    const videoSize = useVideoSize(videoRef)
    // Title chip fades to half opacity after a few seconds so it stops
    // competing with the camera's burned-in OSD; tile hover restores it
    // purely via CSS (group-hover), so there are no re-renders on hover.
    const [titleDimmed, setTitleDimmed] = useState(false)
    useEffect(() => {
      const t = window.setTimeout(() => setTitleDimmed(true), 4000)
      return () => window.clearTimeout(t)
    }, [])

    const isLive = mode === 'live'

    // The aspect the picture is DISPLAYED at, which is not always the aspect it
    // is CODED at: a Dahua-family "1080N" stream is 960x1080 for a 16:9 scene
    // and declares no SAR to correct from. Every box below sizes off this —
    // never off videoWidth/videoHeight directly. See lib/aspect.ts.
    const videoAspect = useMemo(
      () => displayAspect(videoSize?.width, videoSize?.height, displayAspectOverride),
      [videoSize?.width, videoSize?.height, displayAspectOverride]
    )
    // True only when the two disagree, i.e. the frame has to be stretched
    // rather than letterboxed. Drives object-fit on the <video> below.
    const stretched = isStretched(videoSize?.width, videoSize?.height, videoAspect)

    /** JPEG of the current frame at the CORRECTED width. An anamorphic stream
        has to be un-squished in the export too, or the saved file looks nothing
        like the tile it was taken from; drawImage's explicit destination rect
        does the stretch. null when no frame is capturable — callers report why. */
    const captureSnapshot = useCallback((): string | null => {
      const el = videoRef.current
      if (!el || el.readyState < 2) return null
      const codedW = el.videoWidth || el.clientWidth
      const codedH = el.videoHeight || el.clientHeight
      if (!codedW || !codedH) return null
      const aspect = displayAspect(codedW, codedH, displayAspectOverride)
      const { width, height } = snapshotSize(codedW, codedH, aspect)
      const canvas = document.createElement('canvas')
      canvas.width = width
      canvas.height = height
      const ctx = canvas.getContext('2d')
      if (!ctx) return null
      try {
        ctx.drawImage(el, 0, 0, width, height)
      } catch {
        return null
      }
      return canvas.toDataURL('image/jpeg', 0.92)
    }, [displayAspectOverride])

    // Determine available stream types
    const availableStreamTypes: Array<'webrtc' | 'hls'> = []
    if (isLive) {
      if (whepUrl) availableStreamTypes.push('webrtc')
      if (hlsUrl) availableStreamTypes.push('hls')
    }

    // Expose methods via ref
    useImperativeHandle(
      ref,
      () => ({
        play: async () => {
          if (videoRef.current) await videoRef.current.play().catch(() => {})
        },
        pause: () => {
          if (videoRef.current) videoRef.current.pause()
        },
        snapshot: captureSnapshot,
        requestFullscreen: () => {
          if (containerRef.current) {
            const fn =
              (containerRef.current as any).requestFullscreen ||
              (containerRef.current as any).webkitRequestFullscreen ||
              (containerRef.current as any).msRequestFullscreen
            if (fn) fn.call(containerRef.current)
          }
        },
        getVideoElement: () => videoRef.current,
        switchStreamType: (type: 'webrtc' | 'hls') => {
          if (isLive) setStreamType(type)
        },
      }),
      [isLive, captureSnapshot]
    )

    // Cleanup function
    const cleanup = useCallback(() => {
      // Clear any existing error state — except mid-offline-poll, where the
      // overlay must not flash off while a probe attempt tears down/rebuilds.
      if (!offlinePollingRef.current) setError(null)

      // Cancel pending auto-reconnect timers
      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current)
        retryTimerRef.current = null
      }
      if (disconnectGraceRef.current) {
        clearTimeout(disconnectGraceRef.current)
        disconnectGraceRef.current = null
      }
      if (offlinePollTimerRef.current) {
        clearTimeout(offlinePollTimerRef.current)
        offlinePollTimerRef.current = null
      }

      // Cleanup HLS
      if (hlsInstanceRef.current) {
        hlsInstanceRef.current.destroy()
        hlsInstanceRef.current = null
      }
      // Cleanup WebRTC
      if (pcRef.current) {
        // Detach the handler first: an intentional close must not look like a
        // connection drop and trigger an auto-retry.
        pcRef.current.onconnectionstatechange = null
        pcRef.current.getSenders().forEach((s) => {
          try {
            if (s.track) s.track.stop()
          } catch {}
        })
        pcRef.current.close()
        pcRef.current = null
      }
      // DELETE WHEP resource
      if (whepResourceRef.current) {
        fetch(whepResourceRef.current, { method: 'DELETE' }).catch(() => {})
        whepResourceRef.current = null
      }
      // Clear video source completely and reset the element
      if (videoRef.current) {
        const el = videoRef.current
        el.pause()
        el.srcObject = null
        el.removeAttribute('src')
        // Clear any buffered data by loading empty
        el.load()
      }
    }, [])

    // Schedule a bounded, exponentially backed-off restart of the live
    // stream: 2s, 4s, 8s, 16s, 30s — then give up into the manual-retry
    // error overlay. Success paths reset the counter.
    const MAX_AUTO_RETRIES = 5
    const OFFLINE_POLL_INTERVAL_MS = 10000
    const scheduleAutoRetry = useCallback((failMessage: string) => {
      if (mode !== 'live') {
        setError(failMessage)
        setIsLoading(false)
        return
      }
      if (retryTimerRef.current) return // one pending retry at a time
      if (retryCountRef.current >= MAX_AUTO_RETRIES) {
        setIsReconnecting(false)
        setError(failMessage)
        setIsLoading(false)
        return
      }
      const delay = Math.min(2000 * 2 ** retryCountRef.current, 30000)
      retryCountRef.current += 1
      setIsReconnecting(true)
      setError(null)
      retryTimerRef.current = window.setTimeout(() => {
        retryTimerRef.current = null
        restartRef.current()
      }, delay)
    }, [mode])

    // The video-element error listener is bound once, on mount, so it reaches
    // the current scheduler through a ref instead of capturing the one that
    // existed at mount.
    const scheduleAutoRetryRef = useRef(scheduleAutoRetry)
    scheduleAutoRetryRef.current = scheduleAutoRetry

    // Setup WebRTC WHEP
    const setupWebRTC = useCallback(async () => {
      if (!whepUrl || !videoRef.current) return
      if (!offlinePollingRef.current) {
        setIsLoading(true)
        setError(null)
      }

      try {
        // ICE servers come from Settings > More Settings > WebRTC. Falling back
        // to a public STUN keeps playback working if that read fails, but a
        // deployment with TURN configured needs the server's list to traverse
        // NAT at all — so this must not stay hardcoded.
        let iceServers: RTCIceServer[] = [
          { urls: ['stun:stun.l.google.com:19302'] },
        ]
        let policy: RTCIceTransportPolicy | undefined
        let poolSize: number | undefined
        try {
          const { data } = await apiService.getWebRTCClientConfig()
          if (Array.isArray(data?.iceServers) && data.iceServers.length) {
            iceServers = data.iceServers
          }
          if (data?.iceTransportPolicy) policy = data.iceTransportPolicy
          if (typeof data?.iceCandidatePoolSize === 'number') {
            poolSize = data.iceCandidatePoolSize
          }
        } catch {
          // keep the fallback
        }

        const pc = new RTCPeerConnection({
          iceServers,
          ...(policy ? { iceTransportPolicy: policy } : {}),
          ...(poolSize !== undefined ? { iceCandidatePoolSize: poolSize } : {}),
        })
        pcRef.current = pc

        pc.addTransceiver('video', { direction: 'recvonly' })
        pc.addTransceiver('audio', { direction: 'recvonly' })

        pc.ontrack = (ev) => {
          const [stream] = ev.streams
          if (videoRef.current && stream) {
            // Clear any existing error when we get a valid stream
            offlinePollingRef.current = false
            setError(null)
            videoRef.current.srcObject = stream
            videoRef.current.play().catch(() => {})
            setIsLoading(false)
          }
        }

        pc.onconnectionstatechange = () => {
          if (pc.connectionState === 'connected') {
            setIsLoading(false)
            offlinePollingRef.current = false
            setError(null)
            setIsReconnecting(false)
            retryCountRef.current = 0
            if (disconnectGraceRef.current) {
              clearTimeout(disconnectGraceRef.current)
              disconnectGraceRef.current = null
            }
          } else if (
            pc.connectionState === 'failed' ||
            pc.connectionState === 'closed'
          ) {
            scheduleAutoRetry('WebRTC connection failed')
          } else if (pc.connectionState === 'disconnected') {
            // 'disconnected' can self-heal; give ICE a short grace period
            // before tearing down and reconnecting.
            if (!disconnectGraceRef.current) {
              disconnectGraceRef.current = window.setTimeout(() => {
                disconnectGraceRef.current = null
                if (pcRef.current === pc && pc.connectionState !== 'connected') {
                  scheduleAutoRetry('WebRTC connection lost')
                }
              }, 5000)
            }
          }
        }

        const offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        const headers: Record<string, string> = { 'Content-Type': 'application/sdp' }
        if (mediamtxToken) {
          headers['Authorization'] = `Bearer ${mediamtxToken}`
        }
        const resp = await fetch(whepUrl, {
          method: 'POST',
          headers,
          body: offer.sdp || '',
        })

        if (!(resp.status === 200 || resp.status === 201)) {
          // 404 means the stream/path doesn't exist or camera is not streaming
          if (resp.status === 404) {
            throw new Error('Camera offline')
          }
          // MediaMTX rejects an expired/invalid stream JWT with 400/401 on
          // the WHEP POST (observed: 400 for "token is expired"). Retrying
          // with the same token can never succeed, so ask the parent for a
          // fresh one — the changed token prop re-runs setup. The bounded
          // auto-retry below still runs as a fallback for non-auth 400s.
          if (resp.status === 400 || resp.status === 401 || resp.status === 403) {
            onAuthExpiredRef.current?.()
          }
          throw new Error(`WHEP connection failed: ${resp.status}`)
        }

        whepResourceRef.current = resp.headers.get('Location') || null
        const answerSdp = await resp.text()
        await pc.setRemoteDescription({ type: 'answer', sdp: answerSdp })
      } catch (e: any) {
        const msg = e?.message || 'WebRTC setup failed'
        onError?.(msg)
        if (msg === 'Camera offline') {
          // The path doesn't exist server-side — e.g. a MediaMTX restart and
          // this WHEP attempt raced the backend's re-provisioning. The
          // camera-status event remounts the player when the backend notices,
          // but an outage shorter than its offline debounce never emits one,
          // so also poll on a slow cadence until the path comes back.
          setError(msg)
          setIsLoading(false)
          offlinePollingRef.current = true
          if (!offlinePollTimerRef.current) {
            offlinePollTimerRef.current = window.setTimeout(() => {
              offlinePollTimerRef.current = null
              restartRef.current()
            }, OFFLINE_POLL_INTERVAL_MS)
          }
        } else {
          // Any non-404 failure leaves offline-poll mode for the bounded
          // fast-retry path.
          offlinePollingRef.current = false
          scheduleAutoRetry(msg)
        }
      }
    }, [whepUrl, mediamtxToken, onError, scheduleAutoRetry])

    // Setup HLS
    const setupHLS = useCallback(async () => {
      if (!hlsUrl || !videoRef.current) return
      setIsLoading(true)
      setError(null)

      const el = videoRef.current
      el.muted = isMuted
      el.autoplay = autoPlay

      // Build HLS URL with JWT token as query parameter (MediaMTX requirement)
      // MediaMTX accepts JWT tokens via ?jwt=<token> query param
      const hlsUrlWithToken = mediamtxToken
        ? `${hlsUrl}${hlsUrl.includes('?') ? '&' : '?'}jwt=${mediamtxToken}`
        : hlsUrl

      // Native HLS (Safari)
      if (el.canPlayType('application/vnd.apple.mpegurl')) {
        el.src = hlsUrlWithToken
        el.play().catch(() => {})
        setIsLoading(false)
        return
      }

      // HLS.js (lazy-loaded chunk)
      const Hls = await loadHls().catch(() => null)
      if (!videoRef.current) return // unmounted while the chunk loaded
      if (Hls?.isSupported()) {
        const hls = new Hls({
          enableWorker: true,
          lowLatencyMode: false,
          backBufferLength: 90,
          maxBufferLength: 30,
          maxMaxBufferLength: 60,
          startLevel: -1,
          // Auto-recover from media errors
          fragLoadingMaxRetry: 3,
          manifestLoadingMaxRetry: 3,
          // Add Authorization header for MediaMTX JWT auth (fallback)
          xhrSetup: (xhr) => {
            if (mediamtxToken) {
              xhr.setRequestHeader('Authorization', `Bearer ${mediamtxToken}`)
            }
          },
        })
        hlsInstanceRef.current = hls

        hls.loadSource(hlsUrlWithToken)
        hls.attachMedia(el)

        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          setIsLoading(false)
          setError(null) // Clear any previous errors
          setIsReconnecting(false)
          retryCountRef.current = 0
          if (autoPlay) {
            el.play().catch(() => {
              el.muted = true
              el.play().catch(() => {})
            })
          }
        })

        hls.on(Hls.Events.FRAG_LOADED, () => {
          setError(null) // Clear error on successful fragment load
          setIsReconnecting(false)
          retryCountRef.current = 0
          if (el.paused && autoPlay) el.play().catch(() => {})
        })

        hls.on(Hls.Events.ERROR, (_event, data) => {
          console.warn('[HLS] Error:', data.type, data.details, data.fatal)
          if (data.fatal) {
            const msg = `HLS Error: ${data.details}`
            setIsLoading(false)
            onError?.(msg)

            // Auto-recover from errors
            if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
              console.log('[HLS] Attempting media error recovery...')
              hls.recoverMediaError()
            } else if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
              // An expired stream JWT surfaces here as a 400/401 on the
              // manifest/fragment request — same recovery as the WHEP path:
              // a fresh token from the parent, since retrying with the
              // stale one can never succeed.
              const code = data.response?.code
              if (code === 400 || code === 401 || code === 403) {
                onAuthExpiredRef.current?.()
              }
              // Full restart with backoff (a bare startLoad() retry keeps
              // failing while MediaMTX is briefly unreachable).
              console.log('[HLS] Attempting network error recovery...')
              scheduleAutoRetry(msg)
            } else {
              setError(msg)
            }
          }
        })
      } else {
        setError('HLS not supported')
        setIsLoading(false)
      }
    }, [hlsUrl, mediamtxToken, isMuted, autoPlay, onError, scheduleAutoRetry])

    // Setup MP4 playback with optimized loading for fast start
    const setupMP4 = useCallback(() => {
      if (!mp4Url || !videoRef.current) return
      setIsLoading(true)
      setError(null)

      const el = videoRef.current
      
      // Use 'auto' preload to start buffering immediately
      // Combined with server-side range request optimization, this allows faster playback
      el.preload = 'auto'
      el.muted = isMuted
      
      // Start playback as soon as we can (don't wait for full buffer)
      const onCanPlay = () => {
        setIsLoading(false)
        if (autoPlay) {
          el.play().catch(() => {
            // If autoplay fails, try muted
            el.muted = true
            el.play().catch(() => {})
          })
        }
      }
      
      const onLoadedMetadata = () => {
        // Metadata loaded - video dimensions and duration are available
        // Try to start playing immediately if we have any data
        if (el.readyState >= 2) { // HAVE_CURRENT_DATA
          setIsLoading(false)
          if (autoPlay && el.paused) {
            el.play().catch(() => {})
          }
        }
      }
      
      const onProgress = () => {
        // As data buffers, try to start playback ASAP
        if (el.readyState >= 3 && el.paused && autoPlay) { // HAVE_FUTURE_DATA
          setIsLoading(false)
          el.play().catch(() => {})
        }
      }
      
      // Remove listeners after successful playback start
      const onPlaying = () => {
        setIsLoading(false)
        el.removeEventListener('progress', onProgress)
      }
      
      el.addEventListener('canplay', onCanPlay, { once: true })
      el.addEventListener('loadedmetadata', onLoadedMetadata, { once: true })
      el.addEventListener('progress', onProgress)
      el.addEventListener('playing', onPlaying, { once: true })
      
      // Set source - this triggers loading
      el.src = mp4Url
      
      // Load the video (triggers metadata fetch via range request)
      el.load()
    }, [mp4Url, isMuted, autoPlay])

    // Setup HLS VOD playback (for recordings via backend-generated manifest)
    const setupHLSPlayback = useCallback(async () => {
      if (!hlsPlaybackUrl || !videoRef.current) return
      setIsLoading(true)
      setError(null)

      const el = videoRef.current
      el.muted = isMuted
      el.autoplay = autoPlay

      // Prefer hls.js: it fetches our #EXT-X-BYTERANGE VOD fragments as exact
      // closed-range requests, which is what makes deep seeks a single small
      // ranged read. Some browsers' native HLS engines (incl. desktop Chromium
      // that reports canPlayType('...mpegurl') as playable) over-fetch byte-range
      // VOD with open-ended ranges, redundantly streaming toward EOF. So only
      // fall back to native HLS when hls.js isn't available (iOS Safari).
      const Hls = await loadHls().catch(() => null)
      if (!videoRef.current) return // unmounted while the chunk loaded
      if (Hls?.isSupported()) {
        const hls = new Hls({
          enableWorker: true,
          lowLatencyMode: false,
          // VOD-optimized settings
          backBufferLength: 30,
          maxBufferLength: 60,
          maxMaxBufferLength: 120,
          startLevel: -1,
          // Retry settings
          fragLoadingMaxRetry: 4,
          manifestLoadingMaxRetry: 4,
          levelLoadingMaxRetry: 4,
          // No auth headers needed - session ID is in URL
        })
        hlsInstanceRef.current = hls

        hls.loadSource(hlsPlaybackUrl)
        hls.attachMedia(el)

        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          setIsLoading(false)
          setError(null)
          console.log('[HLS Playback] Manifest parsed, starting VOD playback')
          if (autoPlay) {
            el.play().catch(() => {
              el.muted = true
              el.play().catch(() => {})
            })
          }
        })

        hls.on(Hls.Events.FRAG_LOADED, () => {
          setError(null)
          if (el.paused && autoPlay) el.play().catch(() => {})
        })

        hls.on(Hls.Events.ERROR, (_event, data) => {
          console.warn('[HLS Playback] Error:', data.type, data.details, data.fatal)
          if (data.fatal) {
            const msg = `HLS Playback Error: ${data.details}`
            setIsLoading(false)
            onError?.(msg)
            
            // Try to recover from errors
            if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
              console.log('[HLS Playback] Attempting media error recovery...')
              hls.recoverMediaError()
            } else if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
              console.log('[HLS Playback] Network error - triggering fallback to MP4')
              setError(msg)
              // Notify parent to fallback to MP4
              onHlsPlaybackError?.()
            } else {
              setError(msg)
              onHlsPlaybackError?.()
            }
          }
        })
      } else if (el.canPlayType('application/vnd.apple.mpegurl')) {
        // Native HLS fallback (iOS Safari, where hls.js/MSE isn't available).
        el.src = hlsPlaybackUrl
        el.play().catch(() => {})
        setIsLoading(false)
      } else {
        console.warn('[HLS Playback] Neither hls.js nor native HLS available, falling back to MP4')
        setError('HLS not supported')
        setIsLoading(false)
        onHlsPlaybackError?.()
      }
    }, [hlsPlaybackUrl, isMuted, autoPlay, onError, onHlsPlaybackError])

    // Initialize player based on mode and stream type
    useEffect(() => {
      cleanup()
      
      // Small delay to ensure video element is fully reset before loading new source
      const timeoutId = setTimeout(() => {
        if (mode === 'playback') {
          // For playback mode, use HLS if available and preferred, otherwise MP4
          if (streamType === 'hls' && hlsPlaybackUrl) {
            setupHLSPlayback()
          } else if (mp4Url) {
            setupMP4()
          }
        } else if (streamType === 'webrtc' && whepUrl) {
          setupWebRTC()
        } else if (streamType === 'hls' && hlsUrl) {
          setupHLS()
        }
      }, 50)

      return () => {
        clearTimeout(timeoutId)
        cleanup()
      }
    }, [mode, streamType, whepUrl, hlsUrl, mp4Url, hlsPlaybackUrl, cleanup, setupWebRTC, setupHLS, setupMP4, setupHLSPlayback])

    // Video event listeners
    useEffect(() => {
      const el = videoRef.current
      if (!el) return

      const onPlay = () => setIsPlaying(true)
      const onPause = () => setIsPlaying(false)
      // Frames are actually flowing, so any retry state left over from
      // getting here is stale. 'playing' rather than 'play', which only
      // means play() was called and fires even while the stream is stalled.
      //
      // Cancelling the pending timer matters as much as hiding the overlay:
      // a retry scheduled while the stream was failing would otherwise fire
      // afterwards and tear down a stream that has since recovered. The
      // hls.js path clears this itself on MANIFEST_PARSED/FRAG_LOADED, but
      // native playback — which is the path Chrome takes for HLS — has no
      // such hook, so it used to keep showing "Reconnecting…" over a
      // perfectly healthy picture until the operator hit refresh.
      const onPlaying = () => {
        if (retryTimerRef.current) {
          clearTimeout(retryTimerRef.current)
          retryTimerRef.current = null
        }
        retryCountRef.current = 0
        offlinePollingRef.current = false
        setIsReconnecting(false)
        setIsLoading(false)
        setError(null)
      }
      const onTimeUpdate = () => setCurrentTime(el.currentTime)
      const onDurationChange = () => setDuration(el.duration || 0)
      const onLoadedData = () => setIsLoading(false)
      const onWaiting = () => setIsLoading(true)
      const onCanPlay = () => setIsLoading(false)
      const onVolumeChange = () => {
        setIsMuted(el.muted)
        setVolume(el.volume)
      }
      const onProgress = () => {
        if (el.buffered.length > 0) {
          setBuffered(el.buffered.end(el.buffered.length - 1))
        }
      }
      const onError = () => {
        // For WebRTC (srcObject), ignore errors since we use srcObject instead of src
        if (el.srcObject) {
          return
        }
        // Ignore "Empty src attribute" errors - these happen during cleanup/transitions
        const errorMsg = el.error?.message || ''
        if (errorMsg.includes('Empty src') || errorMsg.includes('empty src')) {
          return
        }
        // Only show error if there's no source set at all and we're not loading
        if (!el.src && !el.srcObject) {
          return
        }
        // Go through the retry scheduler rather than straight to the error
        // overlay. A media error on a live stream is usually transient: with
        // hlsAlwaysRemux disabled, MediaMTX only builds an HLS muxer once a
        // client asks for one, so the first request right after switching a
        // tile to HLS can be answered before any segment exists. The native
        // player reports that unparsable body as DEMUXER_ERROR_COULD_NOT_PARSE,
        // which used to park the tile on a manual Retry that succeeded purely
        // because the muxer had warmed up by the time it was clicked. The
        // scheduler still lands on the error overlay once the backoff budget
        // is spent, so a genuinely broken stream is not hidden — and for
        // playback it sets the error immediately, as before.
        scheduleAutoRetryRef.current(errorMsg || 'Video error')
      }

      el.addEventListener('play', onPlay)
      el.addEventListener('playing', onPlaying)
      el.addEventListener('pause', onPause)
      el.addEventListener('timeupdate', onTimeUpdate)
      el.addEventListener('durationchange', onDurationChange)
      el.addEventListener('loadeddata', onLoadedData)
      el.addEventListener('waiting', onWaiting)
      el.addEventListener('canplay', onCanPlay)
      el.addEventListener('volumechange', onVolumeChange)
      el.addEventListener('progress', onProgress)
      el.addEventListener('error', onError)

      return () => {
        el.removeEventListener('play', onPlay)
        el.removeEventListener('playing', onPlaying)
        el.removeEventListener('pause', onPause)
        el.removeEventListener('timeupdate', onTimeUpdate)
        el.removeEventListener('durationchange', onDurationChange)
        el.removeEventListener('loadeddata', onLoadedData)
        el.removeEventListener('waiting', onWaiting)
        el.removeEventListener('canplay', onCanPlay)
        el.removeEventListener('volumechange', onVolumeChange)
        el.removeEventListener('progress', onProgress)
        el.removeEventListener('error', onError)
      }
    }, [])

    // Fullscreen change listener
    useEffect(() => {
      const onFullscreenChange = () => {
        setIsFullscreen(!!document.fullscreenElement)
      }
      document.addEventListener('fullscreenchange', onFullscreenChange)
      return () => document.removeEventListener('fullscreenchange', onFullscreenChange)
    }, [])

    // Auto-hide controls
    useEffect(() => {
      const resetHideTimer = () => {
        setShowControls(true)
        if (hideControlsTimeout.current) clearTimeout(hideControlsTimeout.current)
        hideControlsTimeout.current = window.setTimeout(() => {
          if (isPlaying) setShowControls(false)
        }, 3000)
      }

      const container = containerRef.current
      if (container) {
        container.addEventListener('mousemove', resetHideTimer)
        container.addEventListener('touchstart', resetHideTimer)
      }

      return () => {
        if (container) {
          container.removeEventListener('mousemove', resetHideTimer)
          container.removeEventListener('touchstart', resetHideTimer)
        }
        if (hideControlsTimeout.current) clearTimeout(hideControlsTimeout.current)
      }
    }, [isPlaying])

    // Control handlers
    const handlePlay = () => videoRef.current?.play().catch(() => {})
    const handlePause = () => videoRef.current?.pause()
    const handleMute = () => {
      if (videoRef.current) videoRef.current.muted = true
    }
    const handleUnmute = () => {
      if (videoRef.current) {
        videoRef.current.muted = false
        if (volume === 0) {
          videoRef.current.volume = 0.5
          setVolume(0.5)
        }
      }
    }
    const handleVolumeChange = (vol: number) => {
      if (videoRef.current) {
        videoRef.current.volume = vol
        videoRef.current.muted = vol === 0
      }
    }
    const handleSeek = (time: number) => {
      if (videoRef.current) videoRef.current.currentTime = time
    }
    const handleFullscreen = () => {
      if (isFullscreen) {
        document.exitFullscreen?.()
      } else if (containerRef.current) {
        containerRef.current.requestFullscreen?.()
      }
    }
    const handleSnapshot = () => {
      const el = videoRef.current
      if (!el) {
        console.warn('[VideoPlayer] Snapshot failed: No video element')
        return
      }
      if (el.readyState < 2) {
        console.warn('[VideoPlayer] Snapshot failed: Video not ready, readyState:', el.readyState)
        return
      }
      const dataUrl = captureSnapshot()
      if (!dataUrl) {
        console.warn('[VideoPlayer] Snapshot failed: no frame could be captured')
        return
      }
      console.log('[VideoPlayer] Snapshot captured, length:', dataUrl.length)
      onSnapshot?.(dataUrl)
    }
    const restartStream = () => {
      cleanup()
      if (streamType === 'webrtc' && whepUrl) {
        setupWebRTC()
      } else if (streamType === 'hls' && hlsUrl) {
        setupHLS()
      }
    }
    // Auto-retries go through the ref so scheduleAutoRetry (defined before
    // the setup callbacks) always calls the current stream config.
    restartRef.current = restartStream
    const handleRefresh = () => {
      // Manual retry: start the auto-retry budget over and show the normal
      // loading UX rather than the frozen offline overlay.
      retryCountRef.current = 0
      offlinePollingRef.current = false
      setIsReconnecting(false)
      restartStream()
    }
    const handleStreamTypeChange = (type: 'webrtc' | 'hls') => {
      if (type !== streamType) {
        offlinePollingRef.current = false
        cleanup()
        setStreamType(type)
      }
    }

    // Keyboard shortcuts
    useEffect(() => {
      const handleKeyDown = (e: KeyboardEvent) => {
        if (!containerRef.current?.contains(document.activeElement) && document.activeElement !== document.body) return

        switch (e.key) {
          case ' ':
          case 'k':
            e.preventDefault()
            isPlaying ? handlePause() : handlePlay()
            break
          case 'f':
            e.preventDefault()
            handleFullscreen()
            break
          case 'm':
            e.preventDefault()
            isMuted ? handleUnmute() : handleMute()
            break
          case 'ArrowLeft':
            if (!isLive) {
              e.preventDefault()
              handleSeek(Math.max(0, currentTime - 5))
            }
            break
          case 'ArrowRight':
            if (!isLive) {
              e.preventDefault()
              handleSeek(Math.min(duration, currentTime + 5))
            }
            break
          case 'ArrowUp':
            e.preventDefault()
            handleVolumeChange(Math.min(1, volume + 0.1))
            break
          case 'ArrowDown':
            e.preventDefault()
            handleVolumeChange(Math.max(0, volume - 0.1))
            break
        }
      }

      window.addEventListener('keydown', handleKeyDown)
      return () => window.removeEventListener('keydown', handleKeyDown)
    }, [isPlaying, isMuted, isLive, currentTime, duration, volume])

    // Double-click for fullscreen
    const handleDoubleClick = () => handleFullscreen()

    return (
      <div
        ref={containerRef}
        className={`relative bg-[var(--bg-2)] overflow-hidden group flex items-center justify-center ${className}`}
        style={{ containerType: 'size' }}
        tabIndex={0}
        onDoubleClick={handleDoubleClick}
      >
        {/* Chrome band — full tile width, exactly as tall as the video, and
            vertically centred on it (100cqw = tile width). It is transparent:
            only the feed box inside it paints black, so the residual space
            around the video still shows the app panel background rather than
            letterbox bars.

            The chrome (title, LIVE badge, controls) anchors HERE rather than
            to the video rectangle. For a landscape stream the two are the
            same width, so nothing moves. For a portrait stream the video is a
            narrow strip with wide dead margins either side, and confining the
            control row to that strip left room for only play/mute/fullscreen
            — snapshot and settings dropped out entirely. Spanning the tile
            reclaims the margins, and because the band is only as tall as the
            video the chrome still sits on the feed's own top and bottom edges
            instead of floating below it.

            The band is also the query container the controls size themselves
            against, so the breakpoints now measure the width they actually
            get. 'inline-size' is enough — every query here is width-based. */}
        <div
          className="relative flex justify-center"
          style={
            videoAspect
              ? { width: '100%', height: `min(100%, calc(100cqw / ${videoAspect}))`, containerType: 'inline-size' }
              : { width: '100%', height: '100%', containerType: 'inline-size' }
          }
        >
        {/* Feed box — the video rectangle itself, centred in the band. Width
            follows from the band's height and the stream's aspect ratio.
            min-w-0 keeps the video's intrinsic size from inflating it. */}
        <div
          className="relative h-full min-w-0 bg-black"
          style={videoAspect ? { aspectRatio: String(videoAspect) } : { width: '100%' }}
        >

        {/* Video element. The feed box already carries the stream's DISPLAY
            aspect, so object-fill stretches an anamorphic frame across it with
            nothing cropped — exactly what the DVR's own client does, and a
            no-op whenever the coded and display aspects agree. object-contain
            until metadata lands, when the box has no aspect of its own yet and
            fill would smear the frame across the whole tile. */}
        <video
          ref={videoRef}
          className={`block w-full h-full ${stretched ? 'object-fill' : 'object-contain'}`}
          playsInline
          muted={isMuted}
          autoPlay={autoPlay}
          crossOrigin="anonymous"
          preload={mode === 'playback' ? 'metadata' : 'auto'}
        />

        {/* Loading / reconnecting overlay */}
        {(isLoading || isReconnecting) && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/40">
            <div className="w-10 h-10 border-2 border-white/30 border-t-white rounded-full animate-spin" />
            {isReconnecting && (
              <div className="text-xs text-white/80">Reconnecting…</div>
            )}
          </div>
        )}

        {/* Error overlay */}
        {error && (
          <div className="absolute inset-0 flex flex-col items-center justify-center text-white" style={{ background: '#1e3a8a' }}>
            {/* TV Static background */}
            <div 
              className="absolute inset-0 opacity-20"
              style={{
                background: 'repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(255,255,255,.03) 2px, rgba(255,255,255,.03) 4px), repeating-linear-gradient(90deg, transparent, transparent 2px, rgba(255,255,255,.03) 2px, rgba(255,255,255,.03) 4px)',
                animation: 'tvStatic 0.2s infinite'
              }}
            />
            <style>
              {`
                @keyframes tvStatic {
                  0% { opacity: 0.1; }
                  25% { opacity: 0.15; }
                  50% { opacity: 0.2; }
                  75% { opacity: 0.15; }
                  100% { opacity: 0.1; }
                }
              `}
            </style>
            <div className="absolute inset-0 bg-gradient-to-b from-blue-900/40 via-transparent to-blue-900/40" />
            
            {/* Error content */}
            <div className="relative z-10 flex flex-col items-center">
              <AlertCircle size={48} className="text-blue-200 mb-3" />
              <div className="text-lg font-medium text-white mb-1">{error}</div>
              <div className="text-xs text-blue-200 mb-4">No signal detected</div>
              <button
                onClick={handleRefresh}
                className="px-4 py-2 bg-blue-800/60 hover:bg-blue-700/60 border border-blue-500/50 rounded text-sm transition-colors"
              >
                Retry Connection
              </button>
            </div>
          </div>
        )}

        {/* Caller-provided overlay (PTZ pad etc.) — hugs the feed, since it is
            aimed at the picture rather than the tile */}
        {overlay}
        </div>

        {/* Title overlay — transparent top-centre label (clear of the corner
            OSD burn-ins), legible via text shadow instead of a scrim. Centred
            on the band, which is centred on the video, so it stays visually
            attached to the feed while free to run wider than it — the old cap
            measured a narrow video strip and cut ordinary camera names to a
            few characters.

            The width cap reserves a fixed gutter rather than a percentage.
            Being centred, the title's right edge sits at (100% - gutter/2),
            so a gutter wider than the LIVE badge means the two can never
            collide at any tile size; a percentage cap only holds until the
            tile gets small enough for the badge to outgrow its share. */}
        {title && (
          <div
            className={`absolute top-1.5 left-1/2 -translate-x-1/2 z-10 truncate text-sm @max-[300px]:text-xs leading-tight font-medium text-white/90 px-1.5 py-0.5 [text-shadow:0_1px_3px_rgba(0,0,0,0.9)] transition-opacity duration-700 group-hover:opacity-100 ${
              isLive && !error ? 'max-w-[calc(100%_-_110px)]' : 'max-w-[85%]'
            } ${titleDimmed ? 'opacity-50' : 'opacity-100'}`}
          >
            {title}
          </div>
        )}

        {/* Live indicator — transparent: glowing dot + shadowed text so it
            doesn't overshadow the stream like the old solid red chip. Pinned
            to the band's corner so a long title can never run under it. */}
        {isLive && !error && (
          <div className="absolute top-1.5 right-1.5 z-10 flex items-center gap-1 text-[10px] font-semibold tracking-wider text-red-400 [text-shadow:0_1px_3px_rgba(0,0,0,0.9)]">
            <span className="w-1.5 h-1.5 bg-red-500 rounded-full animate-pulse [box-shadow:0_0_5px_rgba(239,68,68,0.9)]" />
            LIVE
          </div>
        )}

        {/* Custom controls */}
        <div className={`transition-opacity duration-200 ${showControls || !isPlaying ? 'opacity-100' : 'opacity-0'}`}>
          <VideoControls
            videoRef={videoRef}
            isLive={isLive}
            isPlaying={isPlaying}
            isMuted={isMuted}
            volume={volume}
            currentTime={currentTime}
            duration={duration}
            buffered={buffered}
            isFullscreen={isFullscreen}
            isLoading={isLoading}
            streamType={streamType}
            onPlay={handlePlay}
            onPause={handlePause}
            onMute={handleMute}
            onUnmute={handleUnmute}
            onVolumeChange={handleVolumeChange}
            onSeek={handleSeek}
            onFullscreen={handleFullscreen}
            onSnapshot={onSnapshot ? handleSnapshot : undefined}
            onRefresh={isLive ? handleRefresh : undefined}
            onStreamTypeChange={isLive ? handleStreamTypeChange : undefined}
            availableStreamTypes={availableStreamTypes}
            onTogglePtz={onTogglePtz}
            ptzActive={ptzActive}
          />
        </div>
        </div>
      </div>
    )
  }
)
