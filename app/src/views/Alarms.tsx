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

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BellRing, CheckCheck, Volume2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { playTestSound } from '../components/AlertBell'
import { api } from '../lib/api'
import {
  alertsInboxService,
  type InboxAlert,
  type RingConfig,
  type RingMode,
} from '../services/alertsInboxService'

// The Alarms page: every alarm the platform has raised, as a list, plus
// the controls that decide how alarms SOUND and one-click proof that
// the whole chain works. The test button goes through the real
// ingestion path server-side (same table, same poll, same ring as a
// real alert) — a UI-only sound test would pass while the consumer was
// broken, which is exactly the failure this page exists to expose.

const SEVERITIES = ['critical', 'high', 'medium', 'low'] as const
const RING_MODES: RingMode[] = ['none', 'ping', 'continuous']

const SEVERITY_STYLE: Record<string, string> = {
  critical: 'bg-red-600 text-white',
  high: 'bg-orange-600 text-white',
  medium: 'bg-yellow-600 text-black',
  low: 'bg-neutral-600 text-white',
}

export function Alarms() {
  const qc = useQueryClient()
  const [onlyUnacked, setOnlyUnacked] = useState(false)
  const [severityFilter, setSeverityFilter] = useState<string | null>(null)

  const list = useQuery({
    queryKey: ['alarms-page', onlyUnacked, severityFilter],
    queryFn: async () => {
      const { data } = await alertsInboxService.listInboxAlerts({
        unacked: onlyUnacked || undefined,
        severity: severityFilter ?? undefined,
        limit: 200,
      })
      return data as { alerts: InboxAlert[]; unacked_count: number }
    },
    refetchInterval: 10_000,
  })

  const ringCfg = useQuery({
    queryKey: ['alerts-inbox-ring-config'],
    queryFn: async () => {
      const { data } = await alertsInboxService.getRingConfig()
      return data as { ring: RingConfig }
    },
    staleTime: 60_000,
  })

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['alarms-page'] })
    qc.invalidateQueries({ queryKey: ['alerts-inbox-unacked'] })
    qc.invalidateQueries({ queryKey: ['alerts-inbox-page'] })
  }

  const ack = useMutation({
    mutationFn: (ids?: number[]) => alertsInboxService.ackInboxAlerts(ids),
    onSuccess: invalidate,
  })

  const saveRing = useMutation({
    mutationFn: (ring: RingConfig) => alertsInboxService.putRingConfig(ring),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['alerts-inbox-ring-config'] }),
  })

  const testAlarm = useMutation({
    mutationFn: (severity: string) =>
      api.post('/api/v1/alerts-inbox/test', { severity }),
    onSuccess: invalidate,
  })

  const rows = list.data?.alerts ?? []
  const unackedCount = list.data?.unacked_count ?? 0
  const ring = ringCfg.data?.ring

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <BellRing size={18} />
        <h1 className="text-xl font-semibold">Alarms</h1>
        {list.isLoading && (
          <span className="text-xs text-[var(--text-dim)]">Loading…</span>
        )}
      </div>

      {/* Sound policy + working-proof, side by side */}
      <div className="grid md:grid-cols-2 gap-3">
        <div className="border border-[var(--border)] rounded p-3 space-y-2">
          <div className="flex items-center gap-2 font-medium">
            <Volume2 size={14} /> Alarm sound (site-wide)
          </div>
          <div className="text-[12px] text-[var(--text-dim)]">
            none = badge only · ping = one chime on arrival · continuous =
            rings in every open browser until acknowledged
          </div>
          <button
            className="px-2 py-1 rounded border border-neutral-700 hover:bg-[var(--panel-2)] text-sm"
            onClick={playTestSound}
          >
            🔊 Play test sound
          </button>
          <div className="text-[11px] text-[var(--text-dim)]">
            Hear nothing? Check the tab isn't muted and system volume is
            up — this button bypasses every other layer.
          </div>
          {ring && (
            <div className="grid grid-cols-2 gap-2 text-sm">
              {SEVERITIES.map((sev) => (
                <label
                  key={sev}
                  className="flex items-center justify-between gap-2"
                >
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
          )}
        </div>

        <div className="border border-[var(--border)] rounded p-3 space-y-2">
          <div className="font-medium">Verify the alarm chain</div>
          <div className="text-[12px] text-[var(--text-dim)]">
            Fires a clearly-labelled test alarm through the real pipeline —
            it lands in the list below and rings the bell exactly like a
            live one. Acknowledge it to silence.
          </div>
          <div className="flex flex-wrap gap-2">
            {SEVERITIES.map((sev) => (
              <button
                key={sev}
                className={`px-2 py-1 rounded text-sm ${SEVERITY_STYLE[sev]}`}
                disabled={testAlarm.isPending}
                onClick={() => testAlarm.mutate(sev)}
              >
                Test {sev}
              </button>
            ))}
          </div>
          {testAlarm.isError && (
            <div className="text-[12px] text-red-400">
              Test alarm failed — is the backend up to date?
            </div>
          )}
        </div>

        <div className="border border-[var(--border)] rounded p-3 space-y-2 md:col-span-2">
          <div className="font-medium">Arm vehicle alarms (LPR)</div>
          <div className="text-[12px] text-[var(--text-dim)]">
            Vehicle alarm policy lives in the License Plate Recognition
            app:{' '}
            <Link
              to="/app-catalog/license-plate-recognition"
              className="underline hover:text-[var(--text)]"
            >
              open the app
            </Link>{' '}
            → Configure. <span className="text-[var(--text)]">Unknown
            vehicles</span>: enable <code>alarm_on_unknown</code> and add
            your known plates to <code>registry</code> (type a plate,
            press Enter). <span className="text-[var(--text)]">Monitored
            vehicles</span>: add plates to <code>denylist</code> or{' '}
            <code>monitors</code>. Both fire high severity — with the
            sound policy above, they ring until acknowledged.
          </div>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <button
          className={`px-2 py-1 rounded border ${onlyUnacked ? 'bg-[var(--panel-2)] border-[var(--border)]' : 'border-neutral-700'}`}
          onClick={() => setOnlyUnacked((s) => !s)}
        >
          Unacknowledged only
        </button>
        <span className="text-[var(--text-dim)]">Severity:</span>
        <button
          className={`px-2 py-1 rounded border ${severityFilter === null ? 'bg-[var(--panel-2)] border-[var(--border)]' : 'border-neutral-700'}`}
          onClick={() => setSeverityFilter(null)}
        >
          All
        </button>
        {SEVERITIES.map((sev) => (
          <button
            key={sev}
            className={`px-2 py-1 rounded border capitalize ${severityFilter === sev ? 'bg-[var(--panel-2)] border-[var(--border)]' : 'border-neutral-700'}`}
            onClick={() =>
              setSeverityFilter((s) => (s === sev ? null : sev))
            }
          >
            {sev}
          </button>
        ))}
        {unackedCount > 0 && (
          <button
            className="ml-auto inline-flex items-center gap-1 px-2 py-1 rounded border border-neutral-700 hover:bg-[var(--panel-2)]"
            onClick={() => ack.mutate(undefined)}
          >
            <CheckCheck size={13} /> Acknowledge all ({unackedCount})
          </button>
        )}
      </div>

      {/* The list */}
      <div className="border border-[var(--border)] rounded overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-[var(--panel-2)] text-left">
            <tr>
              <th className="px-3 py-2">Severity</th>
              <th className="px-3 py-2">Alarm</th>
              <th className="px-3 py-2">Source</th>
              <th className="px-3 py-2">Camera</th>
              <th className="px-3 py-2">Fired</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={7}
                  className="px-3 py-6 text-center text-[var(--text-dim)]"
                >
                  {list.isLoading
                    ? 'Loading…'
                    : 'No alarms yet — arm a watchlist plate in the LPR app, or fire a test above'}
                </td>
              </tr>
            )}
            {rows.map((a) => (
              <tr key={a.id} className="border-t border-[var(--border)]">
                <td className="px-3 py-2">
                  <span
                    className={`px-1.5 py-0.5 rounded text-[10px] uppercase ${SEVERITY_STYLE[a.severity] ?? SEVERITY_STYLE.low}`}
                  >
                    {a.severity}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <div className="font-medium">{a.title}</div>
                  {a.description && (
                    <div className="text-[11px] text-[var(--text-dim)]">
                      {a.description}
                    </div>
                  )}
                </td>
                <td className="px-3 py-2 text-[var(--text-dim)]">
                  {a.source_name || '—'}
                </td>
                <td className="px-3 py-2 text-[var(--text-dim)]">
                  {a.camera_id || '—'}
                </td>
                <td className="px-3 py-2 text-[var(--text-dim)]">
                  {a.fired_at ? new Date(a.fired_at).toLocaleString() : '—'}
                </td>
                <td className="px-3 py-2">
                  {a.acknowledged_at ? (
                    <span className="text-[var(--text-dim)]">
                      acked {new Date(a.acknowledged_at).toLocaleTimeString()}
                    </span>
                  ) : (
                    <span className="text-red-400">unacked</span>
                  )}
                </td>
                <td className="px-3 py-2 text-right">
                  {!a.acknowledged_at && (
                    <button
                      className="px-2 py-0.5 rounded border border-neutral-700 hover:bg-[var(--panel-2)]"
                      onClick={() => ack.mutate([a.id])}
                    >
                      Ack
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
