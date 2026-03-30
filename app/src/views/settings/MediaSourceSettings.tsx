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

import { useEffect, useState } from 'react'
import { apiService } from '../../lib/apiService'
import { useAuth } from '../../auth/AuthContext'

export function MediaSourceSettings() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Initial state - will be populated by backend API call
  const [cfg, setCfg] = useState<any>({
    mediamtx_base_url: '',
    mediamtx_token: '',
    mediamtx_stream_prefix: 'cam-',
    mediamtx_path_mode: 'id',
    mediamtx_admin_api: '',
    mediamtx_admin_token: '',
    mediamtx_webhook_token: '',
    recordings_base_path: '',
    mediamtx_rtsp_publish_url: '',
    rtsp_proxy_enabled: true,
    ffmpeg_binary_path: 'ffmpeg',
    hls_enabled: true,
    ll_hls_enabled: false,
  })

  const canAdmin = !!me?.is_superuser

  useEffect(() => {
    if (!canAdmin) return
    (async () => {
      try {
        setLoading(true)
        setError(null)
        const { data } = await apiService.getMediaSourceSettings()
        setCfg(data)
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load media source settings')
      } finally {
        setLoading(false)
      }
    })()
  }, [canAdmin])

  const save = async () => {
    if (!canAdmin) return
    try {
      setLoading(true)
      setError(null)
      await apiService.updateMediaSourceSettings(cfg)
      const { data } = await apiService.getMediaSourceSettings()
      setCfg(data)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save media source settings')
    } finally {
      setLoading(false)
    }
  }

  if (!canAdmin) return <div className="text-sm text-amber-400">Admin only.</div>

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
  <h2 className="text-base font-semibold">Media Source Settings</h2>
        <button className="ml-auto px-2 py-1 bg-[var(--accent)] text-white" onClick={save} disabled={loading}>Save</button>
      </div>
      {error && <div className="text-sm text-red-400">{error}</div>}

      <div className="grid grid-cols-2 gap-3 text-sm">
        <div className="border border-neutral-700 bg-[var(--panel-2)] p-2 space-y-2">
          <div className="text-[var(--text-dim)]">Playback Base</div>
          <label className="flex items-center justify-between gap-2">
            <span>Base URL</span>
            <input className="w-80 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.mediamtx_base_url}
              onChange={(e)=>setCfg({...cfg, mediamtx_base_url:e.target.value})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Access Token (query/bearer)</span>
            <input className="w-80 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.mediamtx_token||''}
              onChange={(e)=>setCfg({...cfg, mediamtx_token:e.target.value})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Stream prefix</span>
            <input className="w-40 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.mediamtx_stream_prefix}
              onChange={(e)=>setCfg({...cfg, mediamtx_stream_prefix:e.target.value})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Path mode</span>
            <select className="w-40 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.mediamtx_path_mode}
              onChange={(e)=>setCfg({...cfg, mediamtx_path_mode:e.target.value})}>
              <option value="id">id</option>
              <option value="ip">ip</option>
            </select>
          </label>
        </div>

        <div className="border border-neutral-700 bg-[var(--panel-2)] p-2 space-y-2">
          <div className="text-[var(--text-dim)]">Publishing / RTSP Proxy</div>
          <label className="flex items-center justify-between gap-2">
            <span>RTSP Publish URL</span>
            <input className="w-80 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.mediamtx_rtsp_publish_url}
              onChange={(e)=>setCfg({...cfg, mediamtx_rtsp_publish_url:e.target.value})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>RTSP proxy enabled</span>
            <input type="checkbox" className="accent-[var(--accent)]" checked={!!cfg.rtsp_proxy_enabled}
              onChange={(e)=>setCfg({...cfg, rtsp_proxy_enabled:e.target.checked})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>FFmpeg binary</span>
            <input className="w-60 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.ffmpeg_binary_path||''}
              onChange={(e)=>setCfg({...cfg, ffmpeg_binary_path:e.target.value})} />
          </label>
        </div>

        <div className="border border-neutral-700 bg-[var(--panel-2)] p-2 space-y-2">
          <div className="text-[var(--text-dim)]">Media Server Admin API</div>
          <label className="flex items-center justify-between gap-2">
            <span>Base (with /v3)</span>
            <input className="w-80 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.mediamtx_admin_api||''}
              onChange={(e)=>setCfg({...cfg, mediamtx_admin_api:e.target.value})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Token</span>
            <input className="w-80 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.mediamtx_admin_token||''}
              onChange={(e)=>setCfg({...cfg, mediamtx_admin_token:e.target.value})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Webhook Token</span>
            <input className="w-80 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.mediamtx_webhook_token||''}
              onChange={(e)=>setCfg({...cfg, mediamtx_webhook_token:e.target.value})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Recordings Path</span>
            <input className="w-80 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.recordings_base_path||''}
              onChange={(e)=>setCfg({...cfg, recordings_base_path:e.target.value})} />
          </label>
          <div className="text-[var(--text-dim)] text-xs">Used to provision media-server paths.</div>
        </div>

        <div className="border border-neutral-700 bg-[var(--panel-2)] p-2 space-y-2">
          <div className="text-[var(--text-dim)]">Playback protocols</div>
          <label className="flex items-center justify-between gap-2">
            <span>Enable HLS</span>
            <input type="checkbox" className="accent-[var(--accent)]" checked={!!cfg.hls_enabled}
              onChange={(e)=>setCfg({...cfg, hls_enabled:e.target.checked})} />
          </label>
          <label className="flex items-center justify-between gap-2">
            <span>Enable Low-Latency HLS</span>
            <input type="checkbox" className="accent-[var(--accent)]" checked={!!cfg.ll_hls_enabled}
              onChange={(e)=>setCfg({...cfg, ll_hls_enabled:e.target.checked})} />
          </label>
        </div>

        <div className="border border-neutral-700 bg-[var(--panel-2)] p-2 space-y-2">
          <div className="text-[var(--text-dim)]">Transcoding</div>
          <label className="flex items-center justify-between gap-2">
            <span>Enable transcoding</span>
            <input type="checkbox" className="accent-[var(--accent)]" checked={!!cfg.transcoding_enabled}
              onChange={(e)=>setCfg({...cfg, transcoding_enabled:e.target.checked})} />
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="flex items-center justify-between gap-2">
              <span>Video codec</span>
              <select className="w-40 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.transcode_video_codec||''}
                onChange={(e)=>setCfg({...cfg, transcode_video_codec:e.target.value||null})}>
                <option value="">(auto)</option>
                <option value="h264">h264</option>
                <option value="hevc">hevc</option>
              </select>
            </label>
            <label className="flex items-center justify-between gap-2">
              <span>Audio codec</span>
              <select className="w-40 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.transcode_audio_codec||''}
                onChange={(e)=>setCfg({...cfg, transcode_audio_codec:e.target.value||null})}>
                <option value="">(auto)</option>
                <option value="aac">aac</option>
                <option value="opus">opus</option>
              </select>
            </label>
            <label className="flex items-center justify-between gap-2">
              <span>Video bitrate (kbps)</span>
              <input type="number" className="w-36 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.video_bitrate_kbps??''}
                onChange={(e)=>setCfg({...cfg, video_bitrate_kbps:e.target.value?Number(e.target.value):null})} />
            </label>
            <label className="flex items-center justify-between gap-2">
              <span>Audio bitrate (kbps)</span>
              <input type="number" className="w-36 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.audio_bitrate_kbps??''}
                onChange={(e)=>setCfg({...cfg, audio_bitrate_kbps:e.target.value?Number(e.target.value):null})} />
            </label>
            <label className="flex items-center justify-between gap-2">
              <span>Max FPS</span>
              <input type="number" className="w-28 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.max_fps??''}
                onChange={(e)=>setCfg({...cfg, max_fps:e.target.value?Number(e.target.value):null})} />
            </label>
            <label className="flex items-center justify-between gap-2">
              <span>Scale</span>
              <div className="flex items-center gap-2">
                <input type="number" className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.scale_width??''}
                  onChange={(e)=>setCfg({...cfg, scale_width:e.target.value?Number(e.target.value):null})} />
                <span>×</span>
                <input type="number" className="w-24 bg-[var(--panel)] border border-neutral-700 px-2 py-1" value={cfg.scale_height??''}
                  onChange={(e)=>setCfg({...cfg, scale_height:e.target.value?Number(e.target.value):null})} />
              </div>
            </label>
          </div>
        </div>
      </div>
    </div>
  )
}
