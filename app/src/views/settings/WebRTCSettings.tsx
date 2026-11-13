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
import { extractApiError } from '../../lib/apiError'
import { useTranslation } from '../../i18n'

type TurnServer = { url: string; username?: string; credential?: string }
type Settings = {
  stun_servers: string[]
  turn_servers: TurnServer[]
  transport_policy: 'all' | 'relay'
  public_address: string
  public_ice_port: string
}

/** GET /webrtc/advertised: what MediaMTX tells viewers to connect to. */
type Advertised = {
  hosts: Array<{ host: string; source: 'admin' | 'stun' | 'env' }>
  ice_port: number
  public_ice_port: number
}

/**
 * WebRTC configuration (issue #925).
 *
 * Two things matter for live view from other machines: the addresses the
 * media server advertises (it runs in Docker and cannot see the host's own),
 * and, optionally, STUN/TURN. The addresses come from three places, in this
 * order: the public address set here, the one found by STUN, and the LAN
 * address the launcher detected. The browser adds the address it opened the
 * page on by itself, so nothing here is needed for a LAN viewer.
 *
 * STUN/TURN apply to BOTH the browser player and MediaMTX (MediaMTX is
 * reconfigured on save). Bandwidth/FPS/codec knobs were removed on purpose:
 * a receive-only WHEP viewer cannot set them.
 */
export function WebRTCSettings() {
  const { t } = useTranslation()
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cfg, setCfg] = useState<Settings>({
    stun_servers: [],
    turn_servers: [],
    transport_policy: 'all',
    public_address: '',
    public_ice_port: '',
  })
  const [advertised, setAdvertised] = useState<Advertised | null>(null)

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
        public_address: d.public_address || '',
        public_ice_port: d.public_ice_port ? String(d.public_ice_port) : '',
      })
      try {
        const adv = await apiService.getWebRTCAdvertised()
        setAdvertised(adv.data || null)
      } catch {
        setAdvertised(null) // an older core: the section just hides the list
      }
    } catch (e: any) {
      setError(extractApiError(e, t('settings.failedLoadWebrtc')))
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
      const port = Number(cfg.public_ice_port)
      await apiService.updateWebRTCSettings({
        stun_servers: cfg.stun_servers.map((s) => s.trim()).filter(Boolean),
        turn_servers: cfg.turn_servers,
        transport_policy: cfg.transport_policy,
        public_address: cfg.public_address.trim() || null,
        public_ice_port: port > 0 ? port : null,
      })
      await load()
    } catch (e: any) {
      setError(extractApiError(e, t('settings.failedSaveWebrtc')))
    } finally {
      setLoading(false)
    }
  }

  if (!canAdmin) return <div className="text-sm text-[var(--warn)]">{t('admin.only')}</div>

  const inputCls =
    'flex-1 bg-[var(--panel)] border border-[var(--border)] px-3 py-2  text-sm'
  const btnCls =
    'px-3 py-2 border border-[var(--border)] bg-[var(--panel-2)]  text-sm'
  const icePort = advertised?.ice_port ?? 8189
  const forwardPort = advertised?.public_ice_port ?? icePort

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">WebRTC</h2>
        <button
          className="ml-auto px-4 py-2 bg-[var(--accent)] text-white disabled:opacity-60"
          onClick={save}
          disabled={loading}
        >
          {loading ? t('common.saving') : t('common.save')}
        </button>
      </div>
      {error && <div className="text-sm text-[var(--danger)]">{error}</div>}

      <p className="text-xs text-[var(--text-dim)] max-w-2xl">
        {t('settings.webrtcDescription')}
      </p>

      {/* Reach from outside: the public address, the port to forward, and
          what is advertised right now with where each entry came from. */}
      <div className="border border-[var(--border)] p-3 space-y-3">
        <div className="text-sm font-medium">{t('settings.remoteAccess')}</div>
        <label className="block text-sm">
          <span className="text-[var(--text-dim)]">{t('settings.publicAddress')}</span>
          <input
            className={`${inputCls} block w-full mt-1`}
            value={cfg.public_address}
            placeholder="203.0.113.10 or nvr.example.org"
            onChange={(e) => setCfg({ ...cfg, public_address: e.target.value })}
          />
          <span className="block text-xs text-[var(--text-dim)] mt-1">{t('settings.publicAddressHint')}</span>
        </label>
        <label className="block text-sm">
          <span className="text-[var(--text-dim)]">{t('settings.publicIcePort')}</span>
          <input
            className={`${inputCls} block w-40 mt-1`}
            value={cfg.public_ice_port}
            inputMode="numeric"
            placeholder={String(icePort)}
            onChange={(e) => setCfg({ ...cfg, public_ice_port: e.target.value.replace(/\D/g, '') })}
          />
          <span className="block text-xs text-[var(--text-dim)] mt-1">{t('settings.publicIcePortHint', { port: icePort })}</span>
        </label>
        <p className="text-xs text-[var(--text-dim)]">{t('settings.forwardHint', { port: forwardPort })}</p>
        {advertised && (
          <div className="text-sm">
            <div className="text-[var(--text-dim)]">{t('settings.advertised')}</div>
            {advertised.hosts.length === 0 ? (
              <div className="text-xs text-[var(--warn)] mt-1">{t('settings.advertisedNone')}</div>
            ) : (
              <ul className="mt-1 space-y-0.5 font-mono text-xs">
                {advertised.hosts.map((h) => (
                  <li key={h.host}>
                    {h.host}
                    <span className="ml-2 font-sans text-[var(--text-dim)]">({t(`settings.source.${h.source}`)})</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      {/* STUN */}
      <div className="border border-[var(--border)] p-3 space-y-2">
        <div className="text-sm text-[var(--text-dim)]">{t('settings.stunServers')}</div>
        {cfg.stun_servers.map((s, i) => (
          <div key={i} className="flex items-center gap-2">
            <input
              className={inputCls}
              value={s}
              placeholder="stun:stun.example.org:3478"
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
              {t('common.remove')}
            </button>
          </div>
        ))}
        {cfg.stun_servers.length === 0 && !cfg.public_address.trim() && (
          <div className="text-xs text-[var(--warn)]">{t('settings.stunNone')}</div>
        )}
        <button
          className={btnCls}
          onClick={() => setCfg({ ...cfg, stun_servers: [...cfg.stun_servers, ''] })}
        >
          {t('settings.addStun')}
        </button>
      </div>

      {/* TURN */}
      <div className="border border-[var(--border)] p-3 space-y-2">
        <div className="text-sm text-[var(--text-dim)]">{t('settings.turnServers')}</div>
        {cfg.turn_servers.map((turn, i) => (
          <div key={i} className="grid grid-cols-1 sm:grid-cols-4 gap-2">
            <input
              className={inputCls}
              placeholder="turn:example:3478"
              value={turn.url}
              onChange={(e) => {
                const n = [...cfg.turn_servers]
                n[i] = { ...n[i], url: e.target.value }
                setCfg({ ...cfg, turn_servers: n })
              }}
            />
            <input
              className={inputCls}
              placeholder="username"
              value={turn.username || ''}
              onChange={(e) => {
                const n = [...cfg.turn_servers]
                n[i] = { ...n[i], username: e.target.value }
                setCfg({ ...cfg, turn_servers: n })
              }}
            />
            <input
              className={inputCls}
              placeholder="credential"
              value={turn.credential || ''}
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
              {t('common.remove')}
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
          {t('settings.addTurn')}
        </button>
      </div>

      {/* Transport policy */}
      <div className="border border-[var(--border)] p-3">
        <label className="flex items-center justify-between gap-2 text-sm">
          <span>
            {t('settings.icePolicy')}
            <span className="block text-xs text-[var(--text-dim)]">
              {t('settings.relayHint')}
            </span>
          </span>
          <select
            className="w-40 bg-[var(--panel)] border border-[var(--border)] px-3 py-2 text-sm"
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
