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

import { useEffect, useRef, useState, useCallback } from 'react'
import { Radio } from 'lucide-react'

interface Segment {
  start: string
  duration: number
  playback_url: string
}

interface DvrTimelineProps {
  segments: Segment[]
  isLive: boolean
  currentTime: Date | null
  onSeek: (time: Date, playbackUrl: string) => void
  onGoLive: () => void
  className?: string
}

export function DvrTimeline({
  segments,
  isLive,
  currentTime,
  onSeek,
  onGoLive,
  className = '',
}: DvrTimelineProps) {
  const trackRef = useRef<HTMLDivElement>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [hoverTime, setHoverTime] = useState<Date | null>(null)
  const [hoverX, setHoverX] = useState<number>(0)

  // Calculate timeline bounds (midnight to now)
  const now = new Date()
  const midnight = new Date(now)
  midnight.setHours(0, 0, 0, 0)
  
  const totalMs = now.getTime() - midnight.getTime()
  
  // Convert time to percentage position
  const timeToPercent = useCallback((time: Date) => {
    const ms = time.getTime() - midnight.getTime()
    return Math.max(0, Math.min(100, (ms / totalMs) * 100))
  }, [midnight, totalMs])
  
  // Convert percentage to time
  const percentToTime = useCallback((percent: number) => {
    const ms = (percent / 100) * totalMs
    return new Date(midnight.getTime() + ms)
  }, [midnight, totalMs])

  // Build segment ranges for visualization
  const segmentRanges = segments.map(seg => {
    const start = new Date(seg.start)
    const end = new Date(start.getTime() + seg.duration * 1000)
    return {
      startPercent: timeToPercent(start),
      endPercent: timeToPercent(end),
      segment: seg,
    }
  })

  // Find which segment contains a given time
  const findSegmentAt = useCallback((time: Date): Segment | null => {
    const timeMs = time.getTime()
    for (const seg of segments) {
      const segStart = new Date(seg.start).getTime()
      const segEnd = segStart + seg.duration * 1000
      if (timeMs >= segStart && timeMs < segEnd) {
        return seg
      }
    }
    return null
  }, [segments])

  // Build playback URL for a specific time
  const buildPlaybackUrl = useCallback((time: Date, segment: Segment): string => {
    // Extract base URL from segment's playback_url
    const baseUrl = segment.playback_url.split('?')[0]
    const urlParams = new URLSearchParams(segment.playback_url.split('?')[1])
    const path = urlParams.get('path') || ''
    
    // Format the clicked time as ISO string for MediaMTX
    const startTime = time.toISOString()
    
    // URL-encode the start time (important for + in timezone)
    const encodedStart = encodeURIComponent(startTime)
    
    // Use large duration to play until end of available recordings
    return `${baseUrl}?path=${path}&start=${encodedStart}&duration=86400`
  }, [])

  // Handle click/drag on timeline
  const handleInteraction = useCallback((clientX: number) => {
    if (!trackRef.current) return
    
    const rect = trackRef.current.getBoundingClientRect()
    const x = clientX - rect.left
    const percent = (x / rect.width) * 100
    const time = percentToTime(Math.max(0, Math.min(100, percent)))
    
    // Check if near the live edge (last 1% = go live)
    if (percent >= 99) {
      onGoLive()
      return
    }
    
    // Check if clicking on a recorded segment
    const segment = findSegmentAt(time)
    if (segment) {
      const url = buildPlaybackUrl(time, segment)
      onSeek(time, url)
    }
  }, [percentToTime, findSegmentAt, buildPlaybackUrl, onSeek, onGoLive])

  // Mouse handlers
  const handleMouseDown = (e: React.MouseEvent) => {
    setIsDragging(true)
    handleInteraction(e.clientX)
  }

  const handleMouseMove = (e: React.MouseEvent) => {
    if (!trackRef.current) return
    
    const rect = trackRef.current.getBoundingClientRect()
    const x = e.clientX - rect.left
    const percent = (x / rect.width) * 100
    const time = percentToTime(Math.max(0, Math.min(100, percent)))
    
    setHoverX(x)
    setHoverTime(time)
    
    if (isDragging) {
      handleInteraction(e.clientX)
    }
  }

  const handleMouseUp = () => {
    setIsDragging(false)
  }

  const handleMouseLeave = () => {
    setIsDragging(false)
    setHoverTime(null)
  }

  // Format time for display
  const formatTime = (date: Date) => {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }

  // Current position on timeline
  const currentPercent = currentTime ? timeToPercent(currentTime) : 100

  // Time markers (every 3 hours)
  const timeMarkers = [0, 3, 6, 9, 12, 15, 18, 21].map(hour => {
    const time = new Date(midnight)
    time.setHours(hour)
    return {
      hour,
      percent: timeToPercent(time),
      label: `${hour}:00`,
      visible: time.getTime() <= now.getTime(),
    }
  }).filter(m => m.visible)

  return (
    <div className={`relative ${className}`}>
      {/* Go Live button */}
      <div className="flex items-center justify-between mb-1">
        <span className="text-[10px] text-[var(--text-dim)]">
          {isLive ? (
            <span className="flex items-center gap-1 text-red-400">
              <Radio size={10} className="animate-pulse" /> LIVE
            </span>
          ) : currentTime ? (
            formatTime(currentTime)
          ) : (
            'Today'
          )}
        </span>
        {!isLive && (
          <button
            onClick={onGoLive}
            className="text-[10px] px-2 py-0.5 bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/30 flex items-center gap-1"
          >
            <Radio size={10} /> Go Live
          </button>
        )}
      </div>

      {/* Timeline track */}
      <div
        ref={trackRef}
        className="relative h-6 bg-neutral-800 cursor-pointer select-none"
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseLeave}
      >
        {/* Recorded segments (filled areas) */}
        {segmentRanges.map((range, i) => (
          <div
            key={i}
            className="absolute top-0 bottom-0 bg-[var(--accent)]/40 hover:bg-[var(--accent)]/60 transition-colors"
            style={{
              left: `${range.startPercent}%`,
              width: `${range.endPercent - range.startPercent}%`,
            }}
          />
        ))}

        {/* Current position indicator */}
        <div
          className="absolute top-0 bottom-0 w-0.5 bg-white z-10"
          style={{ left: `${currentPercent}%` }}
        >
          <div className="absolute -top-1 left-1/2 -translate-x-1/2 w-2 h-2 bg-white rounded-full" />
        </div>

        {/* Live edge marker */}
        <div
          className="absolute top-0 bottom-0 w-0.5 bg-red-500"
          style={{ left: '100%', transform: 'translateX(-2px)' }}
        />

        {/* Hover tooltip */}
        {hoverTime && (
          <div
            className="absolute -top-6 px-1 py-0.5 bg-black/80 text-[10px] text-white rounded pointer-events-none z-20 whitespace-nowrap"
            style={{ 
              left: hoverX, 
              transform: 'translateX(-50%)',
            }}
          >
            {formatTime(hoverTime)}
            {!findSegmentAt(hoverTime) && <span className="text-neutral-400 ml-1">(no recording)</span>}
          </div>
        )}
      </div>

      {/* Time markers */}
      <div className="relative h-3 text-[8px] text-[var(--text-dim)]">
        {timeMarkers.map(marker => (
          <span
            key={marker.hour}
            className="absolute transform -translate-x-1/2"
            style={{ left: `${marker.percent}%` }}
          >
            {marker.label}
          </span>
        ))}
        <span className="absolute right-0 text-red-400">NOW</span>
      </div>
    </div>
  )
}
