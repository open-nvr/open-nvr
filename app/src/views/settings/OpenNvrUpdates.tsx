// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Settings › OpenNVR Updates: updates to OpenNVR itself (the host's
 * operating system is the Firmware page).
 *
 * The host does the work (scripts/update-host.*, run by a timer each
 * minute); this page shows what the host found and what it is doing, sets
 * the policy, and asks for an update. While one runs, core itself is
 * replaced, so the page keeps polling through the restart, says it is
 * reconnecting, and offers a reload once the new version answers.
 */

import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Download, ExternalLink, RefreshCw, RotateCcw } from 'lucide-react'
import { useTranslation, useDateFormat } from '../../i18n'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'
import { usePermissions } from '../../hooks/usePermissions'
import { useSnackbar } from '../../components/Snackbar'
import { useConfirm } from '../../components/ui/ConfirmDialog'
import { Badge, Button, ErrorCard, Skeleton } from '../../components/ui'
import { Field, Input, Select } from '../../components/ui/form'
import type { UpdatePolicy, UpdateStatus } from '../../services/updateService'

const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const
const RESULT_BADGE: Record<string, 'success' | 'warning' | 'destructive' | 'neutral'> = {
  succeeded: 'success', rolled_back: 'warning', failed: 'destructive',
}

export function OpenNvrUpdates() {
  const { t } = useTranslation()
  const fmt = useDateFormat()
  const qc = useQueryClient()
  const confirm = useConfirm()
  const { showSuccess, showError } = useSnackbar()
  const { hasPermission } = usePermissions()
  const canUpdate = hasPermission('system.update')

  // Failed polls in a row: while core is being replaced, the API is away.
  const [misses, setMisses] = useState(0)
  const status = useQuery({
    queryKey: ['opennvr-update'],
    queryFn: async () => {
      try {
        const data = (await apiService.getUpdateStatus()).data as UpdateStatus
        setMisses(0)
        return data
      } catch (e) {
        setMisses((m) => m + 1)
        throw e
      }
    },
    // Busy: every 3 s, through the restart. Idle: once a minute.
    refetchInterval: (q) => {
      const s = q.state.data
      return misses > 0 || s?.run?.in_progress || s?.pending_request ? 3_000 : 60_000
    },
    retry: false,
  })
  const s = status.data

  // The version this page was loaded with: a new one answering means the
  // browser is running the old app and should reload.
  const loadedWith = useRef<string | null>(null)
  if (s && loadedWith.current == null) loadedWith.current = s.installed.version
  const newerRunning = !!s && !!loadedWith.current && s.installed.version !== loadedWith.current

  const [policy, setPolicy] = useState<UpdatePolicy | null>(null)
  useEffect(() => { if (s && !policy) setPolicy(s.policy) }, [s, policy])
  const [busy, setBusy] = useState(false)

  if (status.isPending) return <Skeleton className="h-64 w-full" />
  if (!s) {
    return misses > 0 && loadedWith.current
      ? <Reconnecting />
      : <ErrorCard message={extractApiError(status.error, t('upd.loadFailed'))} onRetry={() => status.refetch()} />
  }

  const inProgress = !!s.run?.in_progress
  const offer = s.available
  const mayRequest = canUpdate && s.policy.mode !== 'off' && !!offer && !offer.manual_steps.length
    && !inProgress && !s.pending_request && s.connected

  const requestUpdate = async () => {
    if (!offer) return
    const ok = await confirm({
      title: t('upd.confirmTitle', { version: offer.version }),
      message: t('upd.confirmMessage', { from: s.installed.version, to: offer.version }),
      confirmLabel: t('upd.updateNow'),
    })
    if (!ok) return
    setBusy(true)
    try {
      await apiService.requestUpdate(offer.version)
      showSuccess(t('upd.requested', { version: offer.version }))
      qc.invalidateQueries({ queryKey: ['opennvr-update'] })
    } catch (e) {
      showError(extractApiError(e, t('upd.requestFailed')))
    } finally {
      setBusy(false)
    }
  }
  const cancel = async () => {
    setBusy(true)
    try {
      await apiService.cancelUpdateRequest()
      qc.invalidateQueries({ queryKey: ['opennvr-update'] })
    } catch (e) {
      showError(extractApiError(e, t('upd.requestFailed')))
    } finally {
      setBusy(false)
    }
  }
  const savePolicy = async () => {
    if (!policy) return
    setBusy(true)
    try {
      await apiService.setUpdatePolicy(policy)
      showSuccess(t('upd.policySaved'))
      qc.invalidateQueries({ queryKey: ['opennvr-update'] })
    } catch (e) {
      showError(extractApiError(e, t('upd.policyFailed')))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4 max-w-4xl" data-testid="opennvr-updates">
      <div>
        <h2 className="text-base font-semibold flex items-center gap-2"><Download size={18} /> {t('upd.title')}</h2>
        <p className="mt-1 text-xs text-[var(--text-dim)]">{t('upd.description')}</p>
      </div>

      {misses > 0 && <Reconnecting inline />}
      {newerRunning && (
        <Notice tone="success" icon={<CheckCircle2 size={15} />}>
          <span className="flex-1">{t('upd.newerRunning', { version: s.installed.version })}</span>
          <Button size="sm" variant="primary" onClick={() => window.location.reload()}><RefreshCw size={13} /> {t('upd.reload')}</Button>
        </Notice>
      )}
      {!s.connected && <Notice tone="warning" icon={<AlertTriangle size={15} />}>{t('upd.notConnected')}</Notice>}
      {s.connected && s.policy.mode !== 'off' && !s.runner.running && (
        <Notice tone="warning" icon={<AlertTriangle size={15} />}>
          <span>
            {t('upd.noRunner')}{' '}
            {/* not sudo: start.sh refuses it, and asks for sudo itself for the timer */}
            <code className="font-mono text-[11px]">./start.sh auto-update enable</code>{' · '}
            <code className="font-mono text-[11px]">.\start.ps1 auto-update enable</code>
            {s.runner.last_seen && <> {t('upd.runnerLastSeen', { at: fmt.dateTime(s.runner.last_seen) })}</>}
          </span>
        </Notice>
      )}

      {/* Installed, and what the host found */}
      <section className="border border-[var(--border)] bg-[var(--panel-2)] p-3 space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-[var(--text-dim)]">{t('upd.installed')}</span>
          <span className="font-semibold font-mono" data-testid="upd-installed">{s.installed.version}</span>
          {s.installed.commit && <span className="text-[11px] font-mono text-[var(--text-dim)]">{s.installed.commit.slice(0, 12)}</span>}
          {s.last_check?.at && (
            <span className="ml-auto text-[11px] text-[var(--text-dim)]">{t('upd.lastChecked', { at: fmt.dateTime(s.last_check.at) })}</span>
          )}
        </div>

        {inProgress && s.run ? (
          <Notice tone="info" icon={<RefreshCw size={15} className="animate-spin" />}>
            <span data-testid="upd-progress">{t(`upd.step.${s.run.step}`, { from: s.run.from ?? '', to: s.run.to ?? '' })}</span>
          </Notice>
        ) : s.pending_request ? (
          <Notice tone="info" icon={<RefreshCw size={15} />}>
            <span className="flex-1">{t('upd.pending', { version: s.pending_request.to, who: s.pending_request.requested_by })}</span>
            {canUpdate && <Button size="sm" variant="ghost" onClick={cancel} disabled={busy}>{t('upd.cancel')}</Button>}
          </Notice>
        ) : offer ? (
          <div className="space-y-2" data-testid="upd-available">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold">{t('upd.available', { version: offer.version })}</span>
              {offer.urgent && <Badge variant="destructive">{t('upd.urgent')}</Badge>}
              {offer.notes_url && (
                <a href={offer.notes_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-[var(--accent)] hover:underline">
                  {t('upd.notes')} <ExternalLink size={11} />
                </a>
              )}
              <span className="ml-auto">
                <Button variant="primary" size="sm" onClick={requestUpdate} disabled={!mayRequest || busy} data-testid="upd-update-now"
                  title={!canUpdate ? t('upd.noPermission') : s.policy.mode === 'off' ? t('upd.turnedOff') : undefined}>
                  <Download size={13} /> {t('upd.updateNow')}
                </Button>
              </span>
            </div>
            {offer.manual_steps.length > 0 && (
              <div className="text-xs space-y-1">
                <p className="font-medium text-[var(--warn)]">{t('upd.manualSteps')}</p>
                <ul className="list-disc pl-5 space-y-0.5">{offer.manual_steps.map((m) => <li key={m}>{m}</li>)}</ul>
              </div>
            )}
            {offer.state_migrations && <p className="text-xs text-[var(--warn)]">{t('upd.noAutoRollback')}</p>}
            <p className="text-[11px] text-[var(--text-dim)]">{t('upd.whatHappens')}</p>
          </div>
        ) : (
          <p className="text-sm text-[var(--text-dim)]">
            {s.policy.mode === 'off' ? t('upd.offNothingChecked') : s.last_check ? t('upd.upToDate') : t('upd.notCheckedYet')}
          </p>
        )}

        {s.last_check?.message && s.last_check.result !== 'available' && s.last_check.result !== 'up_to_date' && (
          <details className="text-xs">
            <summary className="cursor-pointer text-[var(--text-dim)]">{t(`upd.result.${s.last_check.result}`)}</summary>
            <pre className="mt-1 whitespace-pre-wrap font-mono text-[11px] bg-[var(--panel)] p-2 border border-[var(--border)]">{s.last_check.message}</pre>
          </details>
        )}
        {s.run && !s.run.in_progress && s.run.step !== 'succeeded' && (
          <Notice tone="warning" icon={<RotateCcw size={15} />}>
            {t(`upd.lastRun.${s.run.step}`, { from: s.run.from ?? '', to: s.run.to ?? '' })}
          </Notice>
        )}
      </section>

      {/* Policy */}
      {policy && (
        <section className="border border-[var(--border)] bg-[var(--panel-2)] p-3 space-y-3" data-testid="upd-policy">
          <h3 className="text-sm font-semibold">{t('upd.policy')}</h3>
          <Field label={t('upd.mode')} hint={t(`upd.modeHint.${policy.mode}`)}>
            <Select value={policy.mode} disabled={!canUpdate} onChange={(e) => setPolicy({ ...policy, mode: e.target.value as UpdatePolicy['mode'] })}>
              {(['off', 'notify', 'manual', 'auto'] as const).map((m) => <option key={m} value={m}>{t(`upd.modeName.${m}`)}</option>)}
            </Select>
          </Field>
          {policy.mode === 'auto' && (
            <>
              <Field label={t('upd.scope')}>
                <Select value={policy.auto_scope} disabled={!canUpdate} onChange={(e) => setPolicy({ ...policy, auto_scope: e.target.value as UpdatePolicy['auto_scope'] })}>
                  <option value="patch">{t('upd.scopePatch')}</option>
                  <option value="minor">{t('upd.scopeMinor')}</option>
                </Select>
              </Field>
              <fieldset className="space-y-1">
                <legend className="text-xs font-medium mb-1">{t('upd.window')}</legend>
                <div className="flex flex-wrap gap-2">
                  {DAYS.map((d) => {
                    const on = policy.window_days.split(',').includes(d)
                    return (
                      <label key={d} className="flex items-center gap-1 text-xs">
                        <input type="checkbox" checked={on} disabled={!canUpdate}
                          onChange={() => {
                            const days = DAYS.filter((x) => (x === d ? !on : policy.window_days.split(',').includes(x)))
                            if (days.length) setPolicy({ ...policy, window_days: days.join(',') })
                          }} />
                        {t(`upd.day.${d}`)}
                      </label>
                    )
                  })}
                </div>
                <div className="flex items-center gap-2 text-xs">
                  <div className="w-24"><Input type="time" value={policy.window_start} disabled={!canUpdate} aria-label={t('upd.windowStart')}
                    onChange={(e) => setPolicy({ ...policy, window_start: e.target.value })} /></div>
                  <span>–</span>
                  <div className="w-24"><Input type="time" value={policy.window_end} disabled={!canUpdate} aria-label={t('upd.windowEnd')}
                    onChange={(e) => setPolicy({ ...policy, window_end: e.target.value })} /></div>
                  <span className="text-[var(--text-dim)]">{t('upd.windowHint')}</span>
                </div>
              </fieldset>
            </>
          )}
          {canUpdate && (
            <Button variant="primary" size="sm" onClick={savePolicy}
              disabled={busy || JSON.stringify(policy) === JSON.stringify(s.policy)}>{t('upd.savePolicy')}</Button>
          )}
        </section>
      )}

      {/* History */}
      {s.history.length > 0 && (
        <section className="border border-[var(--border)] bg-[var(--panel-2)] p-3" data-testid="upd-history">
          <h3 className="text-sm font-semibold mb-2">{t('upd.history')}</h3>
          <ul className="divide-y divide-[var(--border)] text-xs">
            {s.history.map((h) => (
              <li key={`${h.finished}-${h.to}`} className="flex items-center gap-2 py-1.5">
                <span className="w-40 text-[var(--text-dim)] tabular-nums">{fmt.dateTime(h.finished)}</span>
                <span className="font-mono">{h.from} → {h.to}</span>
                <Badge variant={RESULT_BADGE[h.result] ?? 'neutral'} className="ml-auto">{t(`upd.outcome.${h.result}`)}</Badge>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

function Notice({ tone, icon, children }: { tone: 'info' | 'warning' | 'success'; icon: React.ReactNode; children: React.ReactNode }) {
  const color = tone === 'warning' ? 'var(--warn)' : tone === 'success' ? 'var(--success, #16a34a)' : 'var(--accent)'
  return (
    <div className="flex items-center gap-2 p-2 text-sm border"
      style={{ borderColor: `color-mix(in oklab, ${color} 45%, var(--border))`, background: `color-mix(in oklab, ${color} 10%, transparent)` }}>
      <span className="shrink-0" style={{ color }}>{icon}</span>
      {children}
    </div>
  )
}

function Reconnecting({ inline = false }: { inline?: boolean }) {
  const { t } = useTranslation()
  return (
    <div className={inline ? '' : 'p-6'} data-testid="upd-reconnecting">
      <Notice tone="info" icon={<RefreshCw size={15} className="animate-spin" />}>{t('upd.reconnecting')}</Notice>
    </div>
  )
}
