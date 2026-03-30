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

import { api } from '../lib/api'

export type StreamProtocol = 'rtmp' | 'rtmps' | 'srt'
export type VideoCodec = 'copy' | 'libx264' | 'libx265'
export type AudioCodec = 'copy' | 'aac'

export interface CloudStreamTarget {
  target_id?: string  // Optional for create, required for update
  camera_id: number
  enabled: boolean
  server_url: string
  stream_key: string
  protocol: StreamProtocol
  use_tls: boolean
  use_custom_ca: boolean
  video_codec: VideoCodec
  audio_codec: AudioCodec
  video_bitrate: string | null
  audio_bitrate: string | null
  max_reconnect_attempts: number
  reconnect_delay_seconds: number
}

export interface CloudStreamTargetResponse {
  target_id: string
  camera_id: number
  camera_name: string | null
  enabled: boolean
  server_url: string
  stream_key_set: boolean
  protocol: string
  use_tls: boolean
  use_custom_ca: boolean
  video_codec: string
  audio_codec: string
  video_bitrate: string | null
  audio_bitrate: string | null
  max_reconnect_attempts: number
  reconnect_delay_seconds: number
  status: string | null
  running: boolean
}

export interface ServerPreset {
  id: string
  name: string
  description: string
  protocol: string
  default_port: number
  video_codec: string
  audio_codec: string
}

export interface StreamStatus {
  target_id: string
  camera_id: number
  status: string
  running: boolean
  started_at: string | null
  server_url: string | null
  error_message: string | null
  reconnect_attempts: number
}

export const cloudStreamingService = {
  // Server presets (RTMP, RTMPS, SRT)
  getServerPresets: () => 
    api.get('/api/v1/cloud-streaming/presets'),

  // Stream targets (multiple per camera supported)
  listStreamTargets: () => 
    api.get('/api/v1/cloud-streaming/targets'),
  
  getStreamTarget: (targetId: string) =>
    api.get(`/api/v1/cloud-streaming/targets/${targetId}`),
  
  createOrUpdateStreamTarget: (target: CloudStreamTarget) =>
    api.post('/api/v1/cloud-streaming/targets', target),
  
  deleteStreamTarget: (targetId: string) =>
    api.delete(`/api/v1/cloud-streaming/targets/${targetId}`),

  // Stream control
  startStream: (targetId: string) =>
    api.post(`/api/v1/cloud-streaming/targets/${targetId}/start`),
  
  stopStream: (targetId: string) =>
    api.post(`/api/v1/cloud-streaming/targets/${targetId}/stop`),
  
  getStreamStatus: (targetId: string) =>
    api.get(`/api/v1/cloud-streaming/targets/${targetId}/status`),
  
  getAllStreamStatuses: () =>
    api.get('/api/v1/cloud-streaming/status'),
}
