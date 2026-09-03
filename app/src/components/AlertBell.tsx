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

import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, BellOff, Check, CheckCheck, Settings2 } from 'lucide-react'
import {
  alertsInboxService,
  type InboxAlert,
  type RingConfig,
  type RingMode,
} from '../services/alertsInboxService'
import { useClickOutside } from '../hooks/useClickOutside'

const POLL_MS = 10_000

const SEVERITIES = ['critical', 'high', 'medium', 'low'] as const
const RING_MODES: RingMode[] = ['none', 'ping', 'continuous']

const SEVERITY_STYLE: Record<string, string> = {
  critical: 'bg-red-600 text-white',
  high: 'bg-orange-600 text-white',
  medium: 'bg-yellow-600 text-black',
  low: 'bg-neutral-600 text-white',
}

// ── Web Audio ring engine ──────────────────────────────────────────
//
// Same annunciation model as the camera-agent's alarm UI, driven by the
// site-wide ring-config:
//   none       — badge only
//   ping       — one chime when a NEW alert of that severity arrives
//   continuous — a two-tone siren repeats until every alert of that
//                severity is acknowledged
//
// Browsers block audio before the first user gesture; the engine
// resumes its AudioContext on the first click/keypress and the siren
// loop simply starts sounding from that moment — the visual badge and
// red banner are the fallback until then.

let _ctx: AudioContext | null = null

function audioCtx(): AudioContext | null {
  try {
    if (!_ctx) {
      const Ctor =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext })
          .webkitAudioContext
      if (!Ctor) return null
      _ctx = new Ctor()
    }
    if (_ctx.state === 'suspended') void _ctx.resume()
    return _ctx
  } catch {
    return null
  }
}

function tone(freq: number, at: number, dur: number, gainValue = 0.08) {
  const ctx = audioCtx()
  if (!ctx || ctx.state !== 'running') return
  const osc = ctx.createOscillator()
  const gain = ctx.createGain()
  osc.type = 'sine'
  osc.frequency.value = freq
  gain.gain.setValueAtTime(gainValue, ctx.currentTime + at)
  gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + dur)
  osc.connect(gain).connect(ctx.destination)
  osc.start(ctx.currentTime + at)
  osc.stop(ctx.currentTime + at + dur)
}

function playPing() {
  tone(880, 0, 0.25)
}

function playSiren() {
  tone(660, 0, 0.35, 0.1)
  tone(880, 0.4, 0.35, 0.1)
}

/** Resume the shared AudioContext on the first user gesture so a siren
 *  armed before any click becomes audible the moment one happens. */
function useAudioUnlock() {
  useEffect(() => {
    const unlock = () => {
      audioCtx()
      window.removeEventListener('pointerdown', unlock)
      window.removeEventListener('keydown', unlock)
    }
    window.addEventListener('pointerdown', unlock)
    window.addEventListener('keydown', unlock)
    return () => {
      window.removeEventListener('pointerdown', unlock)
      window.removeEventListener('keydown', unlock)
    }
  }, [])
}

function timeAgo(iso: string | null): string {
  if (!iso) return ''
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export function AlertBell() {
  const [open, setOpen] = useState(false)
  const [showConfig, setShowConfig] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)
  useClickOutside(panelRef, open, () => setOpen(false))
  useAudioUnlock()
  const qc = useQueryClient()

  const inbox = useQuery({
    queryKey: ['alerts-inbox-unacked'],
    queryFn: async () => {
      const { data } = await alertsInboxService.listInboxAlerts({
        unacked: true,
        limit: 50,
      })
      return data as { alerts: InboxAlert[]; unacked_count: number }
    },
    refetchInterval: POLL_MS,
  })

  const ringCfg = useQuery({
    queryKey: ['alerts-inbox-ring-config'],
    queryFn: async () => {
      const { data } = await alertsInboxService.getRingConfig()
      return data as { ring: RingConfig }
    },
    staleTime: 60_000,
  })

  const ack = useMutation({
    mutationFn: (ids?: number[]) => alertsInboxService.ackInboxAlerts(ids),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['alerts-inbox-unacked'] }),
  })

  const saveRing = useMutation({
    mutationFn: (ring: RingConfig) => alertsInboxService.putRingConfig(ring),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['alerts-inbox-ring-config'] }),
  })

  const alerts = inbox.data?.alerts ?? []
  const unackedCount = inbox.data?.unacked_count ?? 0
  const ring = ringCfg.data?.ring

  // Ping on NEW arrivals only — never on page load (a shift change must
  // not replay the whole night), tracked by the highest id yet seen.
  const maxSeenId = useRef<number | null>(null)
  useEffect(() => {
    if (!inbox.data) return
    const top = alerts.length ? Math.max(...alerts.map((a) => a.id)) : 0
    if (maxSeenId.current === null) {
      maxSeenId.current = top // baseline: what existed when we opened
      return
    }
    if (top > maxSeenId.current) {
      const fresh = alerts.filter((a) => a.id > (maxSeenId.current as number))
      maxSeenId.current = top
      if (ring && fresh.some((a) => ring[a.severity] === 'ping')) playPing()
    }
  }, [inbox.data]) // eslint-disable-line react-hooks/exhaustive-deps

  // Continuous: siren repeats while ANY unacked alert maps to it.
  const sirenActive = useMemo(
    () => !!ring && alerts.some((a) => ring[a.severity] === 'continuous'),
    [alerts, ring],
  )
  useEffect(() => {
    if (!sirenActive) return
    playSiren()
    const t = window.setInterval(playSiren, 1600)
    return () => window.clearInterval(t)
  }, [sirenActive])

  return (
    <div className="relative" ref={panelRef}>
      <button
        aria-label="Alerts"
        className={`relative inline-flex items-center gap-1 px-2 py-1 rounded ${
          sirenActive
            ? 'bg-red-600 text-white animate-pulse'
            : 'bg-[var(--panel)] hover:bg-[var(--panel-2)]'
        }`}
        onClick={() => setOpen((s) => !s)}
        title="Alerts"
      >
        <Bell size={14} />
        <span className="hidden md:inline">Alerts</span>
        {unackedCount > 0 && (
          <span className="absolute -top-1.5 -right-1.5 min-w-4 h-4 px-1 rounded-full bg-red-600 text-white text-[10px] leading-4 text-center normal-case">
            {unackedCount > 99 ? '99+' : unackedCount}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 mt-1 w-96 max-w-[90vw] bg-[var(--panel)] border border-[var(--border)] text-sm z-50 normal-case tracking-normal shadow-lg">
          <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--border)]">
            <span className="font-semibold">
              Alerts{unackedCount ? ` (${unackedCount})` : ''}
            </span>
            <div className="flex items-center gap-2">
              <button
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded hover:bg-[var(--panel-2)] text-[var(--text-dim)]"
                title="Ring settings"
                onClick={() => setShowConfig((s) => !s)}
              >
                <Settings2 size={13} />
              </button>
              {unackedCount > 0 && (
                <button
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded hover:bg-[var(--panel-2)]"
                  onClick={() => ack.mutate(undefined)}
                  title="Acknowledge all"
                >
                  <CheckCheck size={13} /> Ack all
                </button>
              )}
            </div>
          </div>

          {showConfig && ring && (
            <div className="px-3 py-2 border-b border-[var(--border)] bg-[var(--panel-2)]">
              <div className="text-[var(--text-dim)] mb-1.5">
                Alarm sound per severity (site-wide)
              </div>
              <div className="grid grid-cols-2 gap-1.5">
                {SEVERITIES.map((sev) => (
                  <label key={sev} className="flex items-center justify-between gap-2">
                    <span className="capitalize">{sev}</span>
                    <select
                      className="bg-[var(--panel)] border border-[var(--border)] rounded px-1 py-0.5"
                      value={ring[sev]}
                      onChange={(e) =>
                        saveRing.mutate({
                          ...ring,
                          [sev]: e.target.value as RingMode,
                        })
                      }
                    >
                      {RING_MODES.map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
              <div className="text-[11px] text-[var(--text-dim)] mt-1.5">
                none = badge only · ping = one chime · continuous = rings
                until acknowledged
              </div>
            </div>
          )}

          <div className="max-h-96 overflow-y-auto">
            {alerts.length === 0 ? (
              <div className="px-3 py-6 text-center text-[var(--text-dim)]">
                <BellOff size={18} className="mx-auto mb-1" />
                No unacknowledged alerts
              </div>
            ) : (
              alerts.map((a) => (
                <div
                  key={a.id}
                  className="px-3 py-2 border-b border-[var(--border)] last:border-b-0 flex items-start gap-2"
                >
                  <span
                    className={`mt-0.5 px-1.5 py-0.5 rounded text-[10px] uppercase ${
                      SEVERITY_STYLE[a.severity] ?? SEVERITY_STYLE.low
                    }`}
                  >
                    {a.severity}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="truncate font-medium">{a.title}</div>
                    <div className="text-[11px] text-[var(--text-dim)] truncate">
                      {[a.source_name, a.camera_id, timeAgo(a.fired_at)]
                        .filter(Boolean)
                        .join(' · ')}
                    </div>
                  </div>
                  <button
                    className="p-1 rounded hover:bg-[var(--panel-2)]"
                    title="Acknowledge"
                    onClick={() => ack.mutate([a.id])}
                  >
                    <Check size={14} />
                  </button>
                </div>
              ))
            )}
          </div>

          <div className="px-3 py-2 border-t border-[var(--border)] text-right">
            <Link
              to="/alerts-incidents"
              className="text-[var(--text-dim)] hover:text-[var(--text)]"
              onClick={() => setOpen(false)}
            >
              View all in Alerts &amp; Incidents →
            </Link>
          </div>
        </div>
      )}
    </div>
  )
}
