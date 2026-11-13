// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Bell, ShieldCheck } from 'lucide-react'
import { useTranslation, useDateFormat } from '../../../i18n'
import { useCameras, useUnackedAlerts } from '../../../lib/queries'
import { alarmSeenIso, alarmSeenTitle } from '../../../services/alertsInboxService'
import { SeverityBadge, Skeleton } from '../../../components/ui'
import { WidgetFrame, shortAge } from '../WidgetFrame'

const SEVERITY_INK: Record<string, string> = {
  critical: 'var(--critical)',
  high: 'var(--danger)',
  medium: 'var(--warn)',
  low: 'var(--text-dim)',
}
const SEVERITY_RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 }

/**
 * Open (unacknowledged) alarms, worst first and newest within a severity.
 * Read-only on purpose: acknowledging belongs to the bell and the Alarms
 * page, where the operator sees the evidence before dismissing it.
 */
export function AlarmsWidget({ editing, onRemove }: { editing?: boolean; onRemove?: () => void }) {
  const { t } = useTranslation()
  const fmt = useDateFormat()
  const q = useUnackedAlerts()
  const cams = useCameras()
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(id)
  }, [])

  const camName = useMemo(() => {
    const m = new Map<string, string>()
    for (const c of cams.data?.cameras ?? []) m.set(String(c.id), c.name)
    return m
  }, [cams.data])

  const alerts = useMemo(() => {
    const list = [...(q.data?.alerts ?? [])]
    list.sort((a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9) || b.id - a.id)
    return list
  }, [q.data])
  const unacked = q.data?.unacked_count ?? 0

  return (
    <WidgetFrame
      icon={<Bell size={13} />}
      title={t('dashboard.w.alarms')}
      editing={editing}
      onRemove={onRemove}
      meta={unacked ? <span className="font-mono tabular-nums">{t('dashboard.openCount', { n: unacked })}</span> : undefined}
      actions={<Link to="/alerts-incidents" className="text-[11px] text-[var(--accent)] hover:underline px-1">{t('dashboard.viewAll')}</Link>}
    >
      {q.isPending ? (
        <div className="p-2 space-y-1.5">{Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-9" />)}</div>
      ) : q.isError ? (
        <div className="p-3 text-xs text-[var(--danger)]">{t('dashboard.failedAlarms')}</div>
      ) : alerts.length === 0 ? (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 text-center px-4">
          <ShieldCheck size={26} className="text-[var(--ok)]" />
          <div className="text-sm font-medium text-[var(--text)]">{t('dashboard.allClear')}</div>
          <div className="text-[11px] text-[var(--text-dim)]">{t('dashboard.noOpenAlarms')}</div>
        </div>
      ) : (
        <ul className="absolute inset-0 overflow-y-auto thin-scroll divide-y divide-[var(--border)]">
          {alerts.map((a) => {
            const cam = a.camera_id ? camName.get(a.camera_id) ?? `Camera ${a.camera_id}` : null
            const where = [cam, a.source_name].filter(Boolean).join(' · ')
            return (
              <li key={a.id}>
                <Link
                  to="/alerts-incidents"
                  className="flex items-start gap-2 pl-0 pr-2.5 py-1.5 hover:bg-[var(--panel)]"
                  title={alarmSeenTitle(a, fmt)}
                >
                  <span className="w-[3px] self-stretch shrink-0" style={{ background: SEVERITY_INK[a.severity] ?? 'var(--text-dim)' }} aria-hidden="true" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <SeverityBadge severity={a.severity} className="!px-1 !py-0 !text-[9px]" />
                      <span className="text-[12px] text-[var(--text)] truncate">{a.title}</span>
                    </div>
                    {where && <div className="text-[10.5px] text-[var(--text-dim)] truncate mt-0.5">{where}</div>}
                  </div>
                  <span className="font-mono tabular-nums text-[10.5px] text-[var(--text-dim)] shrink-0 pt-0.5">
                    {shortAge(alarmSeenIso(a), now)}
                  </span>
                </Link>
              </li>
            )
          })}
          {unacked > alerts.length && (
            <li className="px-3 py-1.5 text-[11px] text-[var(--text-dim)]">
              <Link to="/alerts-incidents" className="hover:text-[var(--text)]">+{unacked - alerts.length} {t('dashboard.more')}</Link>
            </li>
          )}
        </ul>
      )}
    </WidgetFrame>
  )
}
