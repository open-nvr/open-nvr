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
} from 'react'
import Hls from 'hls.js'
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
  /** Callback when HLS playback fails (to trigger fallback) */
  onHlsPlaybackError?: () => void
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
      onHlsPlaybackError,
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

    const isLive = mode === 'live'

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
        snapshot: () => {
          const el = videoRef.current
          if (!el || el.readyState < 2) return null
          const w = el.videoWidth || el.clientWidth
          const h = el.videoHeight || el.clientHeight
          if (!w || !h) return null
          const canvas = document.createElement('canvas')
          canvas.width = w
          canvas.height = h
          const ctx = canvas.getContext('2d')
          if (!ctx) return null
          try {
            ctx.drawImage(el, 0, 0, w, h)
          } catch {
            return null
          }
          return canvas.toDataURL('image/jpeg', 0.92)
        },
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
      [isLive]
    )

    // Cleanup function
    const cleanup = useCallback(() => {
      // Clear any existing error state
      setError(null)
      
      // Cleanup HLS
      if (hlsInstanceRef.current) {
        hlsInstanceRef.current.destroy()
        hlsInstanceRef.current = null
      }
      // Cleanup WebRTC
      if (pcRef.current) {
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

    // Setup WebRTC WHEP
    const setupWebRTC = useCallback(async () => {
      if (!whepUrl || !videoRef.current) return
      setIsLoading(true)
      setError(null)

      try {
        const pc = new RTCPeerConnection({
          iceServers: [{ urls: ['stun:stun.l.google.com:19302'] }],
        })
        pcRef.current = pc

        pc.addTransceiver('video', { direction: 'recvonly' })
        pc.addTransceiver('audio', { direction: 'recvonly' })

        pc.ontrack = (ev) => {
          const [stream] = ev.streams
          if (videoRef.current && stream) {
            // Clear any existing error when we get a valid stream
            setError(null)
            videoRef.current.srcObject = stream
            videoRef.current.play().catch(() => {})
            setIsLoading(false)
          }
        }

        pc.onconnectionstatechange = () => {
          if (pc.connectionState === 'connected') {
            setIsLoading(false)
            setError(null)
          } else if (pc.connectionState === 'failed') {
            setError('WebRTC connection failed')
            setIsLoading(false)
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
          throw new Error(`WHEP connection failed: ${resp.status}`)
        }

        whepResourceRef.current = resp.headers.get('Location') || null
        const answerSdp = await resp.text()
        await pc.setRemoteDescription({ type: 'answer', sdp: answerSdp })
      } catch (e: any) {
        const msg = e?.message || 'WebRTC setup failed'
        setError(msg)
        setIsLoading(false)
        onError?.(msg)
      }
    }, [whepUrl, mediamtxToken, onError])

    // Setup HLS
    const setupHLS = useCallback(() => {
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

      // HLS.js
      if (Hls.isSupported()) {
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
          if (autoPlay) {
            el.play().catch(() => {
              el.muted = true
              el.play().catch(() => {})
            })
          }
        })

        hls.on(Hls.Events.FRAG_LOADED, () => {
          setError(null) // Clear error on successful fragment load
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
              console.log('[HLS] Attempting network error recovery...')
              setTimeout(() => hls.startLoad(), 1000)
            } else {
              setError(msg)
            }
          }
        })
      } else {
        setError('HLS not supported')
        setIsLoading(false)
      }
    }, [hlsUrl, mediamtxToken, isMuted, autoPlay, onError])

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
    const setupHLSPlayback = useCallback(() => {
      if (!hlsPlaybackUrl || !videoRef.current) return
      setIsLoading(true)
      setError(null)

      const el = videoRef.current
      el.muted = isMuted
      el.autoplay = autoPlay

      // Native HLS (Safari)
      if (el.canPlayType('application/vnd.apple.mpegurl')) {
        el.src = hlsPlaybackUrl
        el.play().catch(() => {})
        setIsLoading(false)
        return
      }

      // HLS.js for VOD playback
      if (Hls.isSupported()) {
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
      } else {
        console.warn('[HLS Playback] HLS.js not supported, falling back to MP4')
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
        setError(errorMsg || 'Video error')
        setIsLoading(false)
      }

      el.addEventListener('play', onPlay)
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
      const w = el.videoWidth || el.clientWidth
      const h = el.videoHeight || el.clientHeight
      if (!w || !h) {
        console.warn('[VideoPlayer] Snapshot failed: Invalid dimensions', w, h)
        return
      }
      const canvas = document.createElement('canvas')
      canvas.width = w
      canvas.height = h
      const ctx = canvas.getContext('2d')
      if (!ctx) {
        console.warn('[VideoPlayer] Snapshot failed: Could not get canvas context')
        return
      }
      try {
        ctx.drawImage(el, 0, 0, w, h)
        const dataUrl = canvas.toDataURL('image/jpeg', 0.92)
        console.log('[VideoPlayer] Snapshot captured, length:', dataUrl.length)
        onSnapshot?.(dataUrl)
      } catch (err) {
        console.error('[VideoPlayer] Snapshot failed:', err)
      }
    }
    const handleRefresh = () => {
      cleanup()
      if (streamType === 'webrtc' && whepUrl) {
        setupWebRTC()
      } else if (streamType === 'hls' && hlsUrl) {
        setupHLS()
      }
    }
    const handleStreamTypeChange = (type: 'webrtc' | 'hls') => {
      if (type !== streamType) {
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
        className={`relative bg-black overflow-hidden group ${className}`}
        tabIndex={0}
        onDoubleClick={handleDoubleClick}
      >
        {/* Title overlay */}
        {title && (
          <div className="absolute top-2 left-2 z-10 text-sm text-white/90 bg-black/50 px-2 py-0.5 rounded">
            {title}
          </div>
        )}

        {/* Live indicator */}
        {isLive && !error && (
          <div className="absolute top-2 right-2 z-10 flex items-center gap-1.5 text-xs bg-red-600/90 text-white px-2 py-0.5 rounded">
            <span className="w-2 h-2 bg-white rounded-full animate-pulse" />
            LIVE
          </div>
        )}

        {/* Video element */}
        <video
          ref={videoRef}
          className="w-full h-full object-contain"
          playsInline
          muted={isMuted}
          autoPlay={autoPlay}
          crossOrigin="anonymous"
          preload={mode === 'playback' ? 'metadata' : 'auto'}
        />

        {/* Loading overlay */}
        {isLoading && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/40">
            <div className="w-10 h-10 border-2 border-white/30 border-t-white rounded-full animate-spin" />
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
          />
        </div>
      </div>
    )
  }
)
