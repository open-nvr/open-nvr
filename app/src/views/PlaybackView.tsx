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

import React, { useEffect, useState, useCallback, useRef } from 'react'
import { apiService } from '../lib/apiService'
import { useAuth } from '../auth/AuthContext'
import { api } from '../lib/api'
import {
  Calendar,
  Play,
  X,
  Film,
  Clock,
  ChevronRight,
  ChevronDown,
  Camera,
  Loader2,
  AlertCircle,
  PlayCircle,
  Unplug,
  Upload,
} from 'lucide-react'
import { useSnackbar } from '../components/Snackbar'
import { VideoPlayer } from '../components/VideoPlayer/VideoPlayer'

// Daily recording - one entry per camera per day
interface DailyRecording {
  date: string
  total_duration: number
  segment_count: number
  first_start: string
  playback_url: string | null
}

// Camera with its daily recordings
interface CameraWithRecordings {
  camera_id: number
  camera_name: string
  path: string
  recording_count: number  // Number of days
  total_duration: number
  recordings: DailyRecording[]
}

// HLS session response
interface HlsPlaybackSession {
  session_id: string
  manifest_url: string
  camera_id: number
  camera_name: string
  start: string
  end: string
  duration: number
  segment_count: number
  expires_in_seconds: number
}

interface CloudUploadStatus {
  queue_size: number
  worker_running: boolean
  active_file?: string | null
  stats?: {
    queued_total?: number
    completed_total?: number
    failed_total?: number
    retrying_total?: number
    last_error?: { file?: string; message?: string } | null
    last_success?: { file?: string; message?: string } | null
    updated_at?: string | null
  }
}

export function PlaybackView() {
  const { token, loading: authLoading, user } = useAuth()
  const { showError, showSuccess } = useSnackbar()

  // Data states
  const [cameras, setCameras] = useState<CameraWithRecordings[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [totalRecordings, setTotalRecordings] = useState(0)
  const [totalDuration, setTotalDuration] = useState(0)
  const [mediamtxAvailable, setMediamtxAvailable] = useState(true)

  // Expanded cameras (accordion style)
  const [expandedCameras, setExpandedCameras] = useState<Set<number>>(new Set())

  // Playback state
  const [showPlayer, setShowPlayer] = useState(false)
  const [playbackUrl, setPlaybackUrl] = useState<string | null>(null)
  const [hlsPlaybackUrl, setHlsPlaybackUrl] = useState<string | null>(null)
  const [hlsSessionId, setHlsSessionId] = useState<string | null>(null)
  const [playbackMode, setPlaybackMode] = useState<'hls' | 'mp4'>('hls')
  const [playingRecording, setPlayingRecording] = useState<{ camera: string; date: string; duration: number; cameraId: number; firstStart: string } | null>(null)
  const [playbackError, setPlaybackError] = useState<string | null>(null)
  const [playbackLoading, setPlaybackLoading] = useState(false)
  const [cloudUploadStatus, setCloudUploadStatus] = useState<CloudUploadStatus | null>(null)
  const [queueingDayKey, setQueueingDayKey] = useState<string | null>(null)
  const [queuedDayKey, setQueuedDayKey] = useState<string | null>(null)
  const videoRef = useRef<HTMLVideoElement>(null)

  // Load recordings grouped by camera and date
  const loadData = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      if (token) api.setToken(token)
      
      const recordingsRes = await apiService.getRecordingsByDate(undefined, token || undefined)
      
      setCameras(recordingsRes.data?.cameras || [])
      setTotalRecordings(recordingsRes.data?.total_recordings || 0)
      setTotalDuration(recordingsRes.data?.total_duration || 0)
      setMediamtxAvailable(recordingsRes.data?.mediamtx_available !== false)
      
      // Auto-expand first camera if only one exists
      if (recordingsRes.data?.cameras?.length === 1) {
        setExpandedCameras(new Set([recordingsRes.data.cameras[0].camera_id]))
      }
    } catch (err: any) {
      setError(err?.message || 'Failed to load recordings')
      showError('Failed to load recordings')
    } finally {
      setLoading(false)
    }
  }, [token, showError])

  useEffect(() => {
    if (!authLoading) {
      loadData()
    }
  }, [authLoading, loadData])

  useEffect(() => {
    if (!user?.is_superuser) return

    let stopped = false
    let timer: ReturnType<typeof setTimeout> | null = null

    const poll = async () => {
      try {
        const { data } = await apiService.getCloudUploadStatus()
        if (!stopped) {
          setCloudUploadStatus(data)
        }
      } catch {
        // Non-blocking UI: keep playback usable even if status polling fails.
      } finally {
        if (!stopped) {
          timer = setTimeout(poll, 3000)
        }
      }
    }

    poll()
    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
    }
  }, [user?.is_superuser])

  // Toggle camera expansion
  const toggleCamera = (cameraId: number) => {
    setExpandedCameras(prev => {
      const next = new Set(prev)
      if (next.has(cameraId)) {
        next.delete(cameraId)
      } else {
        next.add(cameraId)
      }
      return next
    })
  }

  // Play a daily recording using HLS VOD with fallback to MP4
  const playRecording = async (camera: CameraWithRecordings, recording: DailyRecording) => {
    // Check if playback is available (MediaMTX running)
    if (!recording.playback_url) {
      setPlaybackError('Media source disconnected. MediaMTX playback server is not running.')
      setPlayingRecording({
        camera: camera.camera_name,
        date: recording.date,
        duration: recording.total_duration,
        cameraId: camera.camera_id,
        firstStart: recording.first_start,
      })
      setShowPlayer(true)
      return
    }
    
    setPlaybackError(null)
    setPlaybackLoading(true)
    setPlaybackMode('hls')
    setPlayingRecording({
      camera: camera.camera_name,
      date: recording.date,
      duration: recording.total_duration,
      cameraId: camera.camera_id,
      firstStart: recording.first_start,
    })
    setShowPlayer(true)
    
    try {
      // Calculate end time from first_start + total_duration
      const startDate = new Date(recording.first_start)
      const endDate = new Date(startDate.getTime() + (recording.total_duration * 1000))
      
      // Request HLS session from backend
      const response = await apiService.createHlsPlaybackSession({
        camera_id: camera.camera_id,
        start: recording.first_start,
        end: endDate.toISOString(),
      })
      
      if (response.data && response.data.manifest_url) {
        const session = response.data as HlsPlaybackSession
        setHlsSessionId(session.session_id)
        setHlsPlaybackUrl(session.manifest_url)
        setPlaybackUrl(recording.playback_url) // Keep MP4 URL for fallback
        console.log('[Playback] HLS session created:', session.session_id)
      } else {
        // Fallback to MP4 if HLS session fails
        console.warn('[Playback] HLS session creation returned empty, falling back to MP4')
        setPlaybackMode('mp4')
        setPlaybackUrl(recording.playback_url)
      }
    } catch (err: any) {
      console.warn('[Playback] HLS session creation failed, falling back to MP4:', err?.message)
      // Fallback to direct MP4 URL
      setPlaybackMode('mp4')
      setPlaybackUrl(recording.playback_url)
    } finally {
      setPlaybackLoading(false)
    }
  }

  const uploadRecordingDay = async (camera: CameraWithRecordings, recording: DailyRecording) => {
    if (!user?.is_superuser) {
      showError('Admin privileges required')
      return
    }
    const dayKey = `${camera.camera_id}:${recording.date}`
    try {
      setQueueingDayKey(dayKey)
      const { data } = await apiService.queueCloudUploadForDay(camera.camera_id, recording.date)
      const queued = data?.queued ?? 0
      const skipped = data?.skipped_missing ?? 0
      showSuccess(`Queued ${queued} file(s) for cloud upload${skipped ? ` (${skipped} missing skipped)` : ''}`)
      setQueuedDayKey(dayKey)
      setTimeout(() => {
        setQueuedDayKey((current) => (current === dayKey ? null : current))
      }, 6000)
    } catch (err: any) {
      showError(err?.data?.detail || err?.message || 'Failed to queue cloud upload')
    } finally {
      setQueueingDayKey((current) => (current === dayKey ? null : current))
    }
  }

  // Handle HLS playback error - fallback to MP4
  const handleHlsPlaybackError = useCallback(() => {
    console.warn('[Playback] HLS playback failed, falling back to MP4')
    if (playingRecording && playbackUrl) {
      setPlaybackMode('mp4')
      setHlsPlaybackUrl(null)
    }
  }, [playingRecording, playbackUrl])

  // Close player and cleanup
  const closePlayer = async () => {
    // Cleanup HLS session if one exists
    if (hlsSessionId) {
      try {
        await apiService.deleteHlsPlaybackSession(hlsSessionId)
        console.log('[Playback] HLS session deleted:', hlsSessionId)
      } catch (err) {
        // Ignore cleanup errors
      }
    }
    
    setShowPlayer(false)
    setPlaybackError(null)
    setPlaybackUrl(null)
    setHlsPlaybackUrl(null)
    setHlsSessionId(null)
    setPlayingRecording(null)
    setPlaybackMode('hls')
    if (videoRef.current) {
      videoRef.current.pause()
      videoRef.current.src = ''
    }
  }

  // Format helpers
  const formatDuration = (seconds: number) => {
    const hours = Math.floor(seconds / 3600)
    const mins = Math.floor((seconds % 3600) / 60)
    if (hours > 0) {
      return `${hours}h ${mins}m`
    }
    return `${mins}m`
  }

  const formatDate = (dateStr: string) => {
    const [year, month, day] = dateStr.split('-')
    const date = new Date(parseInt(year), parseInt(month) - 1, parseInt(day))
    return date.toLocaleDateString(undefined, {
      weekday: 'short',
      year: 'numeric',
      month: 'short',
      day: 'numeric'
    })
  }

  const formatTime = (isoStr: string) => {
    return new Date(isoStr).toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit'
    })
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <header className="flex items-center gap-4 flex-wrap">
        <h1 className="text-lg font-semibold flex items-center gap-2">
          <Film size={20} className="text-[var(--accent)]" />
          Recordings
        </h1>
        
        {/* Stats summary */}
        {!loading && cameras.length > 0 && (
          <div className="flex items-center gap-4 text-sm text-[var(--text-dim)]">
            <span className="flex items-center gap-1.5">
              <Film size={14} />
              {totalRecordings} recording{totalRecordings !== 1 ? 's' : ''}
            </span>
            <span className="flex items-center gap-1.5">
              <Clock size={14} />
              {formatDuration(totalDuration)}
            </span>
            <span className="flex items-center gap-1.5">
              <Camera size={14} />
              {cameras.length} camera{cameras.length !== 1 ? 's' : ''}
            </span>
          </div>
        )}
        
        <div className="flex items-center gap-2 ml-auto">
          <button
            onClick={loadData}
            disabled={loading}
            className="px-4 py-1.5 bg-[var(--accent)] text-white text-sm disabled:opacity-50 flex items-center gap-2"
          >
            {loading ? (
              <>
                <Loader2 size={14} className="animate-spin" />
                Loading...
              </>
            ) : (
              'Refresh'
            )}
          </button>
        </div>
      </header>

      {/* Error display */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/20 text-red-400 p-3 text-sm flex items-center gap-2">
          <AlertCircle size={16} />
          {error}
        </div>
      )}

      {/* Media Server disconnected warning */}
      {!loading && !mediamtxAvailable && cameras.length > 0 && (
        <div className="bg-amber-500/10 border border-amber-500/20 text-amber-400 p-3 text-sm flex items-center gap-2">
          <Unplug size={16} />
          <span>
            <strong>Playback server offline.</strong>
          </span>
        </div>
      )}

      {/* Main content - accordion style camera list */}
      {user?.is_superuser && cloudUploadStatus && (cloudUploadStatus.worker_running || cloudUploadStatus.queue_size > 0 || !!cloudUploadStatus.active_file) && (
        <div className="bg-[var(--panel)] border border-neutral-700 p-3">
          <div className="flex items-center justify-between text-sm">
            <div className="font-medium">Cloud Upload Status</div>
            <div className="text-[var(--text-dim)]">
              Queue: {cloudUploadStatus.queue_size} | Completed: {cloudUploadStatus.stats?.completed_total || 0} | Failed: {cloudUploadStatus.stats?.failed_total || 0}
            </div>
          </div>

          <div className="mt-2 h-2 bg-neutral-800 overflow-hidden">
            <div
              className={`h-full transition-all duration-300 ${cloudUploadStatus.worker_running ? 'bg-[var(--accent)] w-2/3 animate-pulse' : 'bg-green-500 w-full'}`}
            />
          </div>

          {cloudUploadStatus.active_file && (
            <div className="mt-2 text-xs text-[var(--text-dim)] truncate">
              Uploading: {cloudUploadStatus.active_file}
            </div>
          )}

          {!!cloudUploadStatus.stats?.last_error?.message && (
            <div className="mt-2 text-xs text-red-400 truncate" title={cloudUploadStatus.stats?.last_error?.message || ''}>
              Last error: {cloudUploadStatus.stats?.last_error?.message}
            </div>
          )}
        </div>
      )}

      <div className="space-y-2">
        {loading ? (
          <div className="bg-[var(--panel)] border border-neutral-700 p-8 text-center">
            <Loader2 size={24} className="animate-spin mx-auto mb-2 text-[var(--accent)]" />
            <p className="text-[var(--text-dim)]">Loading recordings...</p>
          </div>
        ) : cameras.length === 0 ? (
          <div className="bg-[var(--panel)] border border-neutral-700 p-12 text-center">
            <Film size={48} className="mx-auto mb-4 opacity-30" />
            <p className="text-[var(--text-dim)]">No recordings found</p>
            <p className="text-sm text-[var(--text-dim)] mt-1">
              Recordings will appear here once cameras start recording
            </p>
          </div>
        ) : (
          cameras.map((camera) => (
            <div
              key={camera.camera_id}
              className="bg-[var(--panel)] border border-neutral-700 overflow-hidden"
            >
              {/* Camera Header - clickable */}
              <button
                onClick={() => toggleCamera(camera.camera_id)}
                className="w-full px-4 py-3 flex items-center justify-between hover:bg-[var(--panel-2)] transition-colors"
              >
                <div className="flex items-center gap-3">
                  {expandedCameras.has(camera.camera_id) ? (
                    <ChevronDown size={18} className="text-[var(--accent)]" />
                  ) : (
                    <ChevronRight size={18} className="text-[var(--text-dim)]" />
                  )}
                  <Camera size={16} className="text-[var(--accent)]" />
                  <span className="font-medium">{camera.camera_name}</span>
                </div>
                <div className="flex items-center gap-6 text-sm text-[var(--text-dim)]">
                  <span className="flex items-center gap-1.5">
                    <Calendar size={14} />
                    {camera.recording_count} day{camera.recording_count !== 1 ? 's' : ''}
                  </span>
                  <span className="flex items-center gap-1.5">
                    <Clock size={14} />
                    {formatDuration(camera.total_duration)}
                  </span>
                </div>
              </button>

              {/* Daily Recordings List - collapsed by default */}
              {expandedCameras.has(camera.camera_id) && (
                <div className="border-t border-neutral-700">
                  {camera.recordings.map((rec) => (
                    <div
                      key={rec.date}
                      className="px-4 py-3 flex items-center justify-between border-b border-neutral-800 last:border-b-0 hover:bg-[var(--panel-2)] transition-colors"
                    >
                      <div className="flex items-center gap-4 pl-8">
                        <div className="w-10 h-10 bg-[var(--accent)]/10 rounded-lg flex items-center justify-center">
                          <Calendar size={18} className="text-[var(--accent)]" />
                        </div>
                        <div>
                          <div className="font-medium">{formatDate(rec.date)}</div>
                          <div className="text-sm text-[var(--text-dim)] flex items-center gap-3">
                            <span>{formatDuration(rec.total_duration)}</span>
                            <span className="text-xs opacity-60">
                              Started {formatTime(rec.first_start)}
                            </span>
                          </div>
                        </div>
                      </div>

                      <div className="flex items-center gap-2">
                        {user?.is_superuser && (
                          (() => {
                            const dayKey = `${camera.camera_id}:${rec.date}`
                            const isQueueing = queueingDayKey === dayKey
                            const isQueued = queuedDayKey === dayKey
                            return (
                              <button
                                onClick={() => uploadRecordingDay(camera, rec)}
                                className="px-3 py-2 text-sm flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white transition-colors disabled:opacity-60"
                                title={isQueueing ? 'Queueing cloud upload' : isQueued ? 'Queued for cloud upload' : 'Queue this day for cloud upload'}
                                disabled={isQueueing}
                              >
                                {isQueueing ? <Loader2 size={15} className="animate-spin" /> : <Upload size={15} />}
                                {isQueueing ? 'Queueing...' : isQueued ? 'Queued' : 'Upload'}
                              </button>
                            )
                          })()
                        )}

                        <button
                          onClick={() => playRecording(camera, rec)}
                          className={`px-4 py-2 text-sm flex items-center gap-2 transition-colors ${
                            rec.playback_url 
                              ? 'bg-[var(--accent)] hover:bg-[var(--accent)]/80 text-white' 
                              : 'bg-neutral-600 hover:bg-neutral-500 text-neutral-300'
                          }`}
                          title={rec.playback_url ? 'Play recording' : 'Playback unavailable - Media Server offline'}
                        >
                          {rec.playback_url ? (
                            <>
                              <PlayCircle size={16} />
                              Play
                            </>
                          ) : (
                            <>
                              <Unplug size={16} />
                              Offline
                            </>
                          )}
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))
        )}
      </div>

      {/* Video Player Modal */}
      {showPlayer && (playbackUrl || hlsPlaybackUrl || playbackError) && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50 p-4">
          <div className="bg-[var(--panel)] border border-neutral-700 w-full max-w-5xl">
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-neutral-700 bg-[var(--panel-2)]">
              <div className="flex items-center gap-3">
                {playbackError ? (
                  <Unplug size={18} className="text-amber-400" />
                ) : (
                  <Play size={18} className="text-[var(--accent)]" />
                )}
                <div>
                  <h3 className="font-medium flex items-center gap-2">
                    {playingRecording?.camera || 'Playback'}
                    {playbackMode === 'hls' && hlsPlaybackUrl && (
                      <span className="text-xs bg-green-500/20 text-green-400 px-1.5 py-0.5 rounded">HLS</span>
                    )}
                    {playbackMode === 'mp4' && playbackUrl && (
                      <span className="text-xs bg-blue-500/20 text-blue-400 px-1.5 py-0.5 rounded">MP4</span>
                    )}
                  </h3>
                  {playingRecording && (
                    <p className="text-xs text-[var(--text-dim)]">
                      {formatDate(playingRecording.date)} • {formatDuration(playingRecording.duration)}
                    </p>
                  )}
                </div>
              </div>
              <button
                onClick={closePlayer}
                className="p-2 hover:bg-[var(--panel)] rounded transition-colors"
              >
                <X size={18} />
              </button>
            </div>

            {/* Video Player or Error */}
            <div className="bg-black aspect-video flex items-center justify-center">
              {playbackError ? (
                <div className="text-center p-8">
                  <Unplug size={64} className="mx-auto mb-4 text-amber-400 opacity-60" />
                  <h3 className="text-lg font-medium text-amber-400 mb-2">Media Source Disconnected</h3>
                  <p className="text-neutral-400 text-sm max-w-md">
                    The playback server (Media Server) is not running. Recording files exist but cannot be played until the server is started.
                  </p>
                </div>
              ) : playbackLoading ? (
                <div className="text-center">
                  <Loader2 size={48} className="animate-spin mx-auto mb-4 text-[var(--accent)]" />
                  <p className="text-neutral-400">Loading playback...</p>
                </div>
              ) : (
                <VideoPlayer
                  mode="playback"
                  hlsPlaybackUrl={playbackMode === 'hls' ? hlsPlaybackUrl || undefined : undefined}
                  mp4Url={playbackMode === 'mp4' ? playbackUrl || undefined : playbackUrl || undefined}
                  preferredPlaybackType={playbackMode}
                  title={playingRecording?.camera}
                  autoPlay={true}
                  muted={false}
                  className="w-full h-full"
                  onError={(error) => {
                    console.error('[Playback] Video error:', error)
                  }}
                  onHlsPlaybackError={handleHlsPlaybackError}
                />
              )}
            </div>

            {/* Footer */}
            <div className="px-4 py-3 border-t border-neutral-700 bg-[var(--panel-2)]">
              <div className="flex items-center gap-4 text-sm text-[var(--text-dim)]">
                <span className="flex items-center gap-1.5">
                  <Clock size={14} />
                  {playingRecording ? formatDuration(playingRecording.duration) : 'N/A'}
                </span>
                {playbackMode === 'hls' && hlsPlaybackUrl && (
                  <span className="flex items-center gap-1.5 text-green-400">
                    <PlayCircle size={14} />
                    HLS VOD (5s segments)
                  </span>
                )}
                {playbackMode === 'mp4' && (
                  <span className="flex items-center gap-1.5 text-blue-400">
                    <Film size={14} />
                    Direct MP4
                  </span>
                )}
                {playbackError && (
                  <span className="flex items-center gap-1.5 text-amber-400">
                    <AlertCircle size={14} />
                    Playback unavailable
                  </span>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
