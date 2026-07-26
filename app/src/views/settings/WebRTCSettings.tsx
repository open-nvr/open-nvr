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
import { useAuth } from '../../auth/AuthContext'
import { apiService } from '../../lib/apiService'

type TurnServer = { url: string; username?: string; credential?: string }
type Settings = {
  stun_servers: string[]
  turn_servers: TurnServer[]
  transport_policy: 'all' | 'relay'
}

/**
 * WebRTC ICE configuration.
 *
 * Only the controls that actually affect OpenNVR's WHEP playback: STUN/TURN for
 * NAT traversal and the ICE transport policy. Bandwidth/FPS/resolution/codec
 * knobs were intentionally removed — a receive-only WHEP viewer cannot set them;
 * the camera and MediaMTX decide the stream format.
 *
 * These settings apply to BOTH the browser player and the MediaMTX media server
 * (MediaMTX is reconfigured on save).
 */
export function WebRTCSettings() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cfg, setCfg] = useState<Settings>({
    stun_servers: [],
    turn_servers: [],
    transport_policy: 'all',
  })

  const canAdmin = !!me?.is_superuser

  const load = async () => {
    try {
      setLoading(true)
      setError(null)
      const res = await apiService.getWebRTCSettings()
      const d = res.data || {}
      setCfg({
        stun_servers: d.stun_servers || [],
        turn_servers: d.turn_servers || [],
        transport_policy: d.transport_policy || 'all',
      })
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load WebRTC settings')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (canAdmin) load()
  }, [canAdmin])

  const save = async () => {
    try {
      setLoading(true)
      setError(null)
      await apiService.updateWebRTCSettings(cfg)
      await load()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save WebRTC settings')
    } finally {
      setLoading(false)
    }
  }

  if (!canAdmin) return <div className="text-sm text-amber-400">Admin only.</div>

  const inputCls =
    'flex-1 bg-[var(--panel)] border border-[var(--border)] px-3 py-2 rounded text-sm'
  const btnCls =
    'px-3 py-2 border border-[var(--border)] bg-[var(--panel-2)] rounded text-sm'

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">WebRTC</h2>
        <button
          className="ml-auto px-4 py-2 bg-[var(--accent)] text-white rounded disabled:opacity-60"
          onClick={save}
          disabled={loading}
        >
          {loading ? 'Saving…' : 'Save'}
        </button>
      </div>
      {error && <div className="text-sm text-red-400">{error}</div>}

      <p className="text-xs text-[var(--text-dim)] max-w-2xl">
        STUN/TURN let remote viewers reach the stream across NAT. These apply to
        both the browser player and the MediaMTX media server.
      </p>

      {/* STUN */}
      <div className="border border-[var(--border)] rounded p-3 space-y-2">
        <div className="text-sm text-[var(--text-dim)]">STUN servers</div>
        {cfg.stun_servers.map((s, i) => (
          <div key={i} className="flex items-center gap-2">
            <input
              className={inputCls}
              value={s}
              placeholder="stun:stun.l.google.com:19302"
              onChange={(e) => {
                const n = [...cfg.stun_servers]
                n[i] = e.target.value
                setCfg({ ...cfg, stun_servers: n })
              }}
            />
            <button
              className={btnCls}
              onClick={() => {
                const n = [...cfg.stun_servers]
                n.splice(i, 1)
                setCfg({ ...cfg, stun_servers: n })
              }}
            >
              Remove
            </button>
          </div>
        ))}
        <button
          className={btnCls}
          onClick={() => setCfg({ ...cfg, stun_servers: [...cfg.stun_servers, ''] })}
        >
          Add STUN
        </button>
      </div>

      {/* TURN */}
      <div className="border border-[var(--border)] rounded p-3 space-y-2">
        <div className="text-sm text-[var(--text-dim)]">TURN servers</div>
        {cfg.turn_servers.map((t, i) => (
          <div key={i} className="grid grid-cols-1 sm:grid-cols-4 gap-2">
            <input
              className={inputCls}
              placeholder="turn:example:3478"
              value={t.url}
              onChange={(e) => {
                const n = [...cfg.turn_servers]
                n[i] = { ...n[i], url: e.target.value }
                setCfg({ ...cfg, turn_servers: n })
              }}
            />
            <input
              className={inputCls}
              placeholder="username"
              value={t.username || ''}
              onChange={(e) => {
                const n = [...cfg.turn_servers]
                n[i] = { ...n[i], username: e.target.value }
                setCfg({ ...cfg, turn_servers: n })
              }}
            />
            <input
              className={inputCls}
              placeholder="credential"
              value={t.credential || ''}
              onChange={(e) => {
                const n = [...cfg.turn_servers]
                n[i] = { ...n[i], credential: e.target.value }
                setCfg({ ...cfg, turn_servers: n })
              }}
            />
            <button
              className={btnCls}
              onClick={() => {
                const n = [...cfg.turn_servers]
                n.splice(i, 1)
                setCfg({ ...cfg, turn_servers: n })
              }}
            >
              Remove
            </button>
          </div>
        ))}
        <button
          className={btnCls}
          onClick={() =>
            setCfg({
              ...cfg,
              turn_servers: [
                ...cfg.turn_servers,
                { url: 'turn:example:3478', username: '', credential: '' },
              ],
            })
          }
        >
          Add TURN
        </button>
      </div>

      {/* Transport policy */}
      <div className="border border-[var(--border)] rounded p-3">
        <label className="flex items-center justify-between gap-2 text-sm">
          <span>
            ICE transport policy
            <span className="block text-xs text-[var(--text-dim)]">
              "relay" forces traffic through TURN (hides IPs, needs a TURN server)
            </span>
          </span>
          <select
            className="w-40 bg-[var(--panel)] border border-[var(--border)] px-3 py-2 rounded text-sm"
            value={cfg.transport_policy}
            onChange={(e) =>
              setCfg({ ...cfg, transport_policy: e.target.value as 'all' | 'relay' })
            }
          >
            <option value="all">all</option>
            <option value="relay">relay</option>
          </select>
        </label>
      </div>
    </div>
  )
}
