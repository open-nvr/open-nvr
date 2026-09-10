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
import { BellRing, PhoneCall, Volume2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { playTestSound } from '../components/AlertBell'
import { useAuth } from '../auth/AuthContext'
import { api } from '../lib/api'
import {
  alertsInboxService,
  type InboxAlert,
  type RingConfig,
  type RingMode,
} from '../services/alertsInboxService'
import { Button, SeverityBadge } from '../components/ui'
import { Pagination } from '../components/ui/Pagination'
import { AlarmsFilters, AlarmsSelectionBar, AlarmsTable } from '../components/alarms/AlarmsTable'
import { useAckAlarms, useAlarmsList } from '../components/alarms/useAlarmsList'
import { usePagination } from '../hooks/usePagination'
import { useRowSelection } from '../hooks/useRowSelection'

// The Alarms page: every alarm the platform has raised, as a list, plus
// the controls that decide how alarms SOUND and one-click proof that
// the whole chain works. The test button goes through the real
// ingestion path server-side (same table, same poll, same ring as a
// real alert) — a UI-only sound test would pass while the consumer was
// broken, which is exactly the failure this page exists to expose.

const SEVERITIES = ['critical', 'high', 'medium', 'low'] as const
const RING_MODES: RingMode[] = ['none', 'ping', 'continuous']

export function Alarms({ embedded = false }: { embedded?: boolean } = {}) {
  const qc = useQueryClient()
  // The alarm POLICY (ring modes, actions, test alarms) is a site
  // decision — superuser-only on the server; everyone else gets their
  // cameras' alarms and a read-only view of how the site rings.
  const { user } = useAuth()
  const isAdmin = !!user?.is_superuser
  const [onlyUnacked, setOnlyUnacked] = useState(false)
  const [severityFilter, setSeverityFilter] = useState<string | null>(null)
  const pager = usePagination(25, 'alerts-incidents')

  const list = useAlarmsList({
    queryKeyPrefix: 'alarms-page',
    unacked: onlyUnacked,
    severity: severityFilter,
    page: pager.page,
    pageSize: pager.pageSize,
    skip: pager.skip,
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

  // Ticks drop whenever the view changes — a bulk ack must never reach
  // rows the operator can no longer see.
  const sel = useRowSelection<number>({
    unacked: onlyUnacked, severity: severityFilter,
    page: pager.page, size: pager.pageSize,
  })

  const ack = useAckAlarms(() => {
    sel.clear()
    // Acking the last row of the last page would otherwise leave the
    // operator staring at an empty page with no way back.
    if (list.rows.length <= 1 && pager.page > 1) pager.setPage(pager.page - 1)
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

  const rows = list.rows
  const unackedCount = list.unackedCount
  const ring = ringCfg.data?.ring

  return (
    <div className="space-y-4">
      {!embedded && (
        <div className="flex items-center gap-2">
          <BellRing size={18} />
          <h1 className="text-xl font-semibold">Alarms</h1>
          {list.isPending && (
            <span className="text-xs text-[var(--text-dim)]">Loading…</span>
          )}
        </div>
      )}

      {/* Sound policy + working-proof, side by side */}
      <div className="grid md:grid-cols-2 gap-3">
        <div className="border border-[var(--border)] rounded p-3 space-y-2">
          <div className="flex items-center gap-2 font-medium">
            <Volume2 size={14} /> Alarm sound (site-wide)
          </div>
          <div className="text-[12px] text-[var(--text-dim)]">
            none = badge only · ping = one chime on arrival · continuous =
            rings in every open browser until acknowledged. Critical rings
            a siren wail; other severities a two-tone beep — and an
            unacknowledged critical always overrides the beep.
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
                    className="bg-[var(--panel)] border border-[var(--border)] rounded px-1 py-0.5 disabled:opacity-60"
                    value={ring[sev]}
                    disabled={!isAdmin}
                    title={isAdmin ? undefined : 'Only an administrator can change the site alarm policy'}
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

        {isAdmin && (
        <div className="border border-[var(--border)] rounded p-3 space-y-2">
          <div className="font-medium">Verify the alarm chain</div>
          <div className="text-[12px] text-[var(--text-dim)]">
            Fires a clearly-labelled test alarm through the real pipeline —
            it lands in the list below and rings the bell exactly like a
            live one. Acknowledge it to silence.
          </div>
          <div className="flex flex-wrap gap-2">
            {SEVERITIES.map((sev) => (
              <Button
                key={sev}
                variant="outline"
                disabled={testAlarm.isPending}
                onClick={() => testAlarm.mutate(sev)}
              >
                Test <SeverityBadge severity={sev} />
              </Button>
            ))}
          </div>
          {testAlarm.isError && (
            <div className="text-[12px] text-red-400">
              Test alarm failed — is the backend up to date?
            </div>
          )}
        </div>
        )}

        {isAdmin && (
        <div className="border border-[var(--border)] rounded p-3 space-y-2 md:col-span-2">
          <div className="font-medium">Arm vehicle alarms (LPR)</div>
          <div className="text-[12px] text-[var(--text-dim)]">
            Vehicle alarm policy lives in the ANPR — License Plate Recognition
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
        )}

        {isAdmin && <AlarmActionsCard />}
      </div>

      {/* The list */}
      <AlarmsTable
        caption="Alerts and incidents"
        rows={rows}
        showSource
        selected={sel.selected}
        onToggle={sel.toggle}
        onToggleAll={(on) => sel.toggleMany(rows.map((a) => a.id), on)}
        onAck={(ids) => ack.mutate(ids)}
        isPending={list.isPending}
        isFetching={list.isFetching}
        isError={list.isError}
        error={list.error}
        onRetry={() => list.refetch()}
        emptyTitle={severityFilter || onlyUnacked
          ? 'No alarms match these filters'
          : 'No alarms yet'}
        emptyDescription="Arm a watchlist plate in the LPR app, or fire a test above."
        emptyAction={(severityFilter || onlyUnacked) ? (
          <Button variant="outline" onClick={() => {
            setSeverityFilter(null); setOnlyUnacked(false); pager.setPage(1)
          }}>Clear filters</Button>
        ) : undefined}
        toolbar={
          <>
            <div className="flex flex-wrap items-center gap-2 py-1.5 pl-3">
              <AlarmsFilters
                onlyUnacked={onlyUnacked}
                onToggleUnacked={() => { setOnlyUnacked((v) => !v); pager.setPage(1) }}
                severity={severityFilter}
                onSeverity={(sev: string | null) => { setSeverityFilter(sev); pager.setPage(1) }}
              />
              <AlarmsSelectionBar
                count={sel.count}
                allOnPage={rows.length > 0 && rows.every((a) => sel.has(a.id))}
                allMatching={sel.allMatching}
                matchingTotal={list.total}
                onSelectAllMatching={sel.selectAllMatching}
                onClear={sel.clear}
                onAck={() => ack.mutate(
                  sel.allMatching
                    ? (severityFilter ? { severity: severityFilter } : {})
                    : [...sel.selected],
                )}
              />
              <div className="ml-auto">
                <Pagination
                  page={pager.page}
                  pageSize={pager.pageSize}
                  total={list.total}
                  rowCount={rows.length}
                  hasNext={rows.length === pager.pageSize}
                  isFetching={list.isFetching}
                  label="alarms"
                  onPageChange={pager.setPage}
                  onPageSizeChange={pager.setPageSize}
                />
              </div>
            </div>
          </>
        }
      />
    </div>
  )
}

// ── Call & external alarm (hooter) configuration ───────────────────
//
// What happens when NOBODY has a browser open: at or above the chosen
// severity, place a Twilio voice call / SMS to the configured numbers
// and/or hit an external relay URL (a hooter or speaker behind a
// Shelly/Tasmota/Node-RED style HTTP switch). The auth token is
// write-only — stored encrypted server-side, never displayed again.
// SIP trunk calling is planned; Twilio Voice covers "the phone rings"
// today.

type ActionsShape = {
  min_severity: string
  twilio: {
    enabled: boolean
    account_sid: string
    auth_token_set?: boolean
    from_number: string
    to_numbers: string[]
    mode: 'call' | 'sms' | 'both'
  }
  webhook: { enabled: boolean; url: string; method: 'POST' | 'GET' }
}

function AlarmActionsCard() {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<ActionsShape | null>(null)
  const [token, setToken] = useState('')
  const [testOut, setTestOut] = useState<
    { action: string; ok: boolean; detail: string }[] | string | null
  >(null)

  const cfg = useQuery({
    queryKey: ['alarm-actions'],
    queryFn: async () => {
      const { data } = await alertsInboxService.getAlarmActions()
      return data as { actions: ActionsShape }
    },
    staleTime: 30_000,
  })

  const current = draft ?? cfg.data?.actions ?? null

  const save = useMutation({
    mutationFn: async () => {
      if (!current) return
      const payload: Record<string, unknown> = {
        min_severity: current.min_severity,
        twilio: {
          ...current.twilio,
          ...(token.trim() ? { auth_token: token.trim() } : {}),
        },
        webhook: current.webhook,
      }
      await alertsInboxService.putAlarmActions(payload)
    },
    onSuccess: () => {
      setToken('')
      setDraft(null)
      qc.invalidateQueries({ queryKey: ['alarm-actions'] })
    },
  })

  const test = useMutation({
    mutationFn: async () => {
      const { data } = await alertsInboxService.testAlarmActions()
      return data as {
        results: { action: string; ok: boolean; detail: string }[]
        note?: string
      }
    },
    onSuccess: (d) => setTestOut(d.results.length ? d.results : d.note ?? ''),
    onError: () => setTestOut('test request failed — backend up to date?'),
  })

  const patch = (p: Partial<ActionsShape>) =>
    current && setDraft({ ...current, ...p })
  const patchTw = (p: Partial<ActionsShape['twilio']>) =>
    current && setDraft({ ...current, twilio: { ...current.twilio, ...p } })
  const patchWh = (p: Partial<ActionsShape['webhook']>) =>
    current && setDraft({ ...current, webhook: { ...current.webhook, ...p } })

  if (!current) return null
  return (
    <div className="border border-[var(--border)] rounded p-3 space-y-3 md:col-span-2">
      <div className="flex items-center gap-2 font-medium">
        <PhoneCall size={14} /> Call &amp; external alarm (beyond the browser)
      </div>
      <div className="text-[12px] text-[var(--text-dim)]">
        At or above the severity below, OpenNVR can phone/SMS the guard
        (Twilio) and trigger an external hooter or speaker behind an HTTP
        relay — even when no browser is open. SIP trunk calling is
        planned; Twilio Voice covers calls today.
      </div>

      <label className="flex items-center gap-2 text-sm">
        Act at or above
        <select
          className="bg-[var(--panel)] border border-[var(--border)] rounded px-1 py-0.5"
          value={current.min_severity}
          onChange={(e) => patch({ min_severity: e.target.value })}
        >
          {['medium', 'high', 'critical'].map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </label>

      <div className="grid md:grid-cols-2 gap-3 text-sm">
        <div className="space-y-1.5 border border-[var(--border)] rounded p-2">
          <label className="flex items-center gap-2 font-medium">
            <input
              type="checkbox"
              checked={current.twilio.enabled}
              onChange={(e) => patchTw({ enabled: e.target.checked })}
            />
            Phone call / SMS (Twilio)
          </label>
          <input
            className="w-full px-2 py-1 rounded border border-[var(--border)] bg-[var(--bg-2)]"
            placeholder="Account SID (ACxxxxxxxx…)"
            value={current.twilio.account_sid}
            onChange={(e) => patchTw({ account_sid: e.target.value })}
          />
          <input
            className="w-full px-2 py-1 rounded border border-[var(--border)] bg-[var(--bg-2)]"
            type="password"
            placeholder={
              current.twilio.auth_token_set
                ? 'Auth token ✓ saved — enter to replace'
                : 'Auth token'
            }
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <input
            className="w-full px-2 py-1 rounded border border-[var(--border)] bg-[var(--bg-2)]"
            placeholder="From number (+1…)"
            value={current.twilio.from_number}
            onChange={(e) => patchTw({ from_number: e.target.value })}
          />
          <input
            className="w-full px-2 py-1 rounded border border-[var(--border)] bg-[var(--bg-2)]"
            placeholder="To numbers, comma-separated (+91…, +91…)"
            value={current.twilio.to_numbers.join(', ')}
            onChange={(e) =>
              patchTw({
                to_numbers: e.target.value.split(',').map((n) => n.trim())
                  .filter(Boolean),
              })
            }
          />
          <label className="flex items-center gap-2">
            Mode
            <select
              className="bg-[var(--panel)] border border-[var(--border)] rounded px-1 py-0.5"
              value={current.twilio.mode}
              onChange={(e) =>
                patchTw({ mode: e.target.value as 'call' | 'sms' | 'both' })
              }
            >
              <option value="call">call</option>
              <option value="sms">sms</option>
              <option value="both">call + sms</option>
            </select>
          </label>
        </div>

        <div className="space-y-1.5 border border-[var(--border)] rounded p-2">
          <label className="flex items-center gap-2 font-medium">
            <input
              type="checkbox"
              checked={current.webhook.enabled}
              onChange={(e) => patchWh({ enabled: e.target.checked })}
            />
            External hooter / speaker (HTTP relay)
          </label>
          <input
            className="w-full px-2 py-1 rounded border border-[var(--border)] bg-[var(--bg-2)]"
            placeholder="Relay URL (http://192.168.1.50/relay/0?turn=on)"
            value={current.webhook.url}
            onChange={(e) => patchWh({ url: e.target.value })}
          />
          <label className="flex items-center gap-2">
            Method
            <select
              className="bg-[var(--panel)] border border-[var(--border)] rounded px-1 py-0.5"
              value={current.webhook.method}
              onChange={(e) =>
                patchWh({ method: e.target.value as 'POST' | 'GET' })
              }
            >
              <option value="POST">POST (JSON alarm payload)</option>
              <option value="GET">GET (dumb relay trigger)</option>
            </select>
          </label>
          <div className="text-[11px] text-[var(--text-dim)]">
            Works with Shelly, Tasmota, Node-RED, or any siren relay that
            accepts an HTTP request.
          </div>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          className="px-3 py-1 rounded bg-blue-600 text-white text-sm disabled:opacity-50"
          disabled={save.isPending || (!draft && !token.trim())}
          onClick={() => save.mutate()}
        >
          {save.isPending ? 'Saving…' : 'Save actions'}
        </button>
        <button
          className="px-3 py-1 rounded border border-neutral-700 hover:bg-[var(--panel-2)] text-sm disabled:opacity-50"
          disabled={test.isPending}
          onClick={() => test.mutate()}
          title="Runs every ENABLED action once with a synthetic alarm"
        >
          {test.isPending ? 'Testing…' : 'Send test action'}
        </button>
        {save.isError && (
          <span className="text-[12px] text-red-400">save failed</span>
        )}
      </div>
      {testOut !== null && (
        <div className="text-[12px] space-y-0.5">
          {typeof testOut === 'string' ? (
            <div className="text-[var(--text-dim)]">{testOut || 'no actions enabled'}</div>
          ) : (
            testOut.map((r, i) => (
              <div key={i} className={r.ok ? 'text-green-400' : 'text-red-400'}>
                {r.ok ? '✓' : '✗'} {r.action}: {r.detail}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}
