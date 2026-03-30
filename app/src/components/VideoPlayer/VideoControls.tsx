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

import { useRef, useEffect, useState, useCallback } from 'react'
import {
  Play, Pause, Volume2, VolumeX, Maximize, Minimize,
  Settings, Camera, SkipBack, SkipForward, RefreshCw,
  Radio, Loader2
} from 'lucide-react'

export interface VideoControlsProps {
  videoRef: React.RefObject<HTMLVideoElement | null>
  isLive?: boolean
  isPlaying: boolean
  isMuted: boolean
  volume: number
  currentTime: number
  duration: number
  buffered: number
  isFullscreen: boolean
  isLoading?: boolean
  streamType?: 'webrtc' | 'hls' | 'mp4'
  onPlay: () => void
  onPause: () => void
  onMute: () => void
  onUnmute: () => void
  onVolumeChange: (volume: number) => void
  onSeek: (time: number) => void
  onFullscreen: () => void
  onSnapshot?: () => void
  onRefresh?: () => void
  onStreamTypeChange?: (type: 'webrtc' | 'hls') => void
  availableStreamTypes?: Array<'webrtc' | 'hls'>
}

export function VideoControls({
  videoRef,
  isLive = false,
  isPlaying,
  isMuted,
  volume,
  currentTime,
  duration,
  buffered,
  isFullscreen,
  isLoading = false,
  streamType,
  onPlay,
  onPause,
  onMute,
  onUnmute,
  onVolumeChange,
  onSeek,
  onFullscreen,
  onSnapshot,
  onRefresh,
  onStreamTypeChange,
  availableStreamTypes = [],
}: VideoControlsProps) {
  const [showVolumeSlider, setShowVolumeSlider] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [isDragging, setIsDragging] = useState(false)
  const progressRef = useRef<HTMLDivElement>(null)
  const volumeTimeoutRef = useRef<number | null>(null)
  const settingsRef = useRef<HTMLDivElement>(null)

  // Format time as MM:SS or HH:MM:SS
  const formatTime = (seconds: number): string => {
    if (!isFinite(seconds) || seconds < 0) return '0:00'
    const h = Math.floor(seconds / 3600)
    const m = Math.floor((seconds % 3600) / 60)
    const s = Math.floor(seconds % 60)
    if (h > 0) {
      return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
    }
    return `${m}:${s.toString().padStart(2, '0')}`
  }

  // Progress bar click/drag handling
  const handleProgressClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (isLive || !progressRef.current || !duration) return
    const rect = progressRef.current.getBoundingClientRect()
    const percent = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
    onSeek(percent * duration)
  }, [isLive, duration, onSeek])

  const handleProgressDrag = useCallback((e: MouseEvent) => {
    if (!isDragging || isLive || !progressRef.current || !duration) return
    const rect = progressRef.current.getBoundingClientRect()
    const percent = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
    onSeek(percent * duration)
  }, [isDragging, isLive, duration, onSeek])

  useEffect(() => {
    if (isDragging) {
      window.addEventListener('mousemove', handleProgressDrag)
      window.addEventListener('mouseup', () => setIsDragging(false))
      return () => {
        window.removeEventListener('mousemove', handleProgressDrag)
        window.removeEventListener('mouseup', () => setIsDragging(false))
      }
    }
  }, [isDragging, handleProgressDrag])

  // Close settings dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (settingsRef.current && !settingsRef.current.contains(e.target as Node)) {
        setShowSettings(false)
      }
    }
    if (showSettings) {
      document.addEventListener('mousedown', handleClickOutside)
      return () => document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [showSettings])

  const progressPercent = duration > 0 ? (currentTime / duration) * 100 : 0
  const bufferedPercent = duration > 0 ? (buffered / duration) * 100 : 0

  return (
    <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/90 via-black/60 to-transparent pt-8 pb-2 px-3 transition-opacity group-hover:opacity-100 opacity-0">
      {/* Progress bar (hidden for live) */}
      {!isLive && (
        <div
          ref={progressRef}
          className="h-1 bg-white/20 rounded-full mb-3 cursor-pointer group/progress relative"
          onClick={handleProgressClick}
          onMouseDown={() => setIsDragging(true)}
        >
          {/* Buffered */}
          <div
            className="absolute inset-y-0 left-0 bg-white/30 rounded-full"
            style={{ width: `${bufferedPercent}%` }}
          />
          {/* Progress */}
          <div
            className="absolute inset-y-0 left-0 bg-[var(--accent)] rounded-full"
            style={{ width: `${progressPercent}%` }}
          />
          {/* Thumb */}
          <div
            className="absolute top-1/2 -translate-y-1/2 w-3 h-3 bg-[var(--accent)] rounded-full opacity-0 group-hover/progress:opacity-100 transition-opacity shadow-md"
            style={{ left: `calc(${progressPercent}% - 6px)` }}
          />
        </div>
      )}

      {/* Controls row */}
      <div className="flex items-center gap-2">
        {/* Play/Pause */}
        <button
          onClick={isPlaying ? onPause : onPlay}
          className="p-1.5 hover:bg-white/20 rounded transition-colors"
          title={isPlaying ? 'Pause' : 'Play'}
        >
          {isLoading ? (
            <Loader2 size={20} className="animate-spin" />
          ) : isPlaying ? (
            <Pause size={20} />
          ) : (
            <Play size={20} />
          )}
        </button>

        {/* Skip buttons (playback only) */}
        {!isLive && (
          <>
            <button
              onClick={() => onSeek(Math.max(0, currentTime - 10))}
              className="p-1.5 hover:bg-white/20 rounded transition-colors"
              title="Back 10s"
            >
              <SkipBack size={18} />
            </button>
            <button
              onClick={() => onSeek(Math.min(duration, currentTime + 10))}
              className="p-1.5 hover:bg-white/20 rounded transition-colors"
              title="Forward 10s"
            >
              <SkipForward size={18} />
            </button>
          </>
        )}

        {/* Refresh (live only) */}
        {isLive && onRefresh && (
          <button
            onClick={onRefresh}
            className="p-1.5 hover:bg-white/20 rounded transition-colors"
            title="Refresh stream"
          >
            <RefreshCw size={18} />
          </button>
        )}

        {/* Volume */}
        <div
          className="relative flex items-center"
          onMouseEnter={() => {
            if (volumeTimeoutRef.current) clearTimeout(volumeTimeoutRef.current)
            setShowVolumeSlider(true)
          }}
          onMouseLeave={() => {
            volumeTimeoutRef.current = window.setTimeout(() => setShowVolumeSlider(false), 300)
          }}
        >
          <button
            onClick={isMuted ? onUnmute : onMute}
            className="p-1.5 hover:bg-white/20 rounded transition-colors"
            title={isMuted ? 'Unmute' : 'Mute'}
          >
            {isMuted || volume === 0 ? <VolumeX size={18} /> : <Volume2 size={18} />}
          </button>
          <div className={`flex items-center overflow-hidden transition-all duration-200 ${showVolumeSlider ? 'w-20 ml-1' : 'w-0'}`}>
            <input
              type="range"
              min="0"
              max="1"
              step="0.05"
              value={isMuted ? 0 : volume}
              onChange={(e) => onVolumeChange(parseFloat(e.target.value))}
              className="w-full h-1 bg-white/30 rounded-full appearance-none cursor-pointer accent-[var(--accent)]"
            />
          </div>
        </div>

        {/* Time display */}
        <div className="text-xs text-white/80 ml-2 font-mono">
          {isLive ? (
            <span className="flex items-center gap-1.5 text-red-400">
              <Radio size={12} className="animate-pulse" />
              LIVE
            </span>
          ) : (
            <>
              {formatTime(currentTime)} / {formatTime(duration)}
            </>
          )}
        </div>

        {/* Spacer */}
        <div className="flex-1" />

        {/* Stream type indicator (live only) */}
        {isLive && streamType && (
          <span className="text-[10px] uppercase tracking-wider bg-white/20 px-1.5 py-0.5 rounded">
            {streamType}
          </span>
        )}

        {/* Snapshot */}
        {onSnapshot && (
          <button
            onClick={onSnapshot}
            className="p-1.5 hover:bg-white/20 rounded transition-colors"
            title="Take snapshot"
          >
            <Camera size={18} />
          </button>
        )}

        {/* Settings */}
        {(availableStreamTypes.length > 1 || !isLive) && (
          <div className="relative" ref={settingsRef}>
            <button
              onClick={() => setShowSettings(!showSettings)}
              className="p-1.5 hover:bg-white/20 rounded transition-colors"
              title="Settings"
            >
              <Settings size={18} />
            </button>
            {showSettings && (
              <div className="absolute bottom-full right-0 mb-2 bg-[var(--panel)] border border-neutral-700 rounded shadow-lg min-w-[140px] py-1 text-sm">
                {/* Stream type switcher (live only) */}
                {isLive && availableStreamTypes.length > 1 && onStreamTypeChange && (
                  <div className="px-3 py-1.5 border-b border-neutral-700">
                    <div className="text-[10px] uppercase text-[var(--text-dim)] mb-1">Stream</div>
                    {availableStreamTypes.map((type) => (
                      <button
                        key={type}
                        onClick={() => {
                          onStreamTypeChange(type)
                          setShowSettings(false)
                        }}
                        className={`block w-full text-left px-2 py-1 rounded text-xs ${streamType === type ? 'bg-[var(--accent)]/30 text-[var(--accent)]' : 'hover:bg-white/10'}`}
                      >
                        {type.toUpperCase()}
                      </button>
                    ))}
                  </div>
                )}
                {/* Playback speed (playback only) */}
                {!isLive && videoRef.current && (
                  <div className="px-3 py-1.5">
                    <div className="text-[10px] uppercase text-[var(--text-dim)] mb-1">Speed</div>
                    {[0.5, 1, 1.5, 2].map((speed) => (
                      <button
                        key={speed}
                        onClick={() => {
                          if (videoRef.current) videoRef.current.playbackRate = speed
                          setShowSettings(false)
                        }}
                        className={`block w-full text-left px-2 py-1 rounded text-xs ${videoRef.current?.playbackRate === speed ? 'bg-[var(--accent)]/30 text-[var(--accent)]' : 'hover:bg-white/10'}`}
                      >
                        {speed}x
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Fullscreen */}
        <button
          onClick={onFullscreen}
          className="p-1.5 hover:bg-white/20 rounded transition-colors"
          title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
        >
          {isFullscreen ? <Minimize size={18} /> : <Maximize size={18} />}
        </button>
      </div>
    </div>
  )
}
