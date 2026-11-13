// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { ShieldAlert } from 'lucide-react'
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis } from 'recharts'
import { useTranslation, useDateFormat } from '../../../i18n'
import { useSuricataStats } from '../../../lib/queries'
import { Skeleton } from '../../../components/ui'
import { WidgetFrame, CHART_TOOLTIP_STYLE } from '../WidgetFrame'

/**
 * Network intrusion alerts from Suricata, when the IDS is deployed. Off by
 * default: most installs do not run it, and a widget that only ever says
 * "unavailable" is noise on the main screen.
 */
export function NetworkIdsWidget({ editing, onRemove }: { editing?: boolean; onRemove?: () => void }) {
  const { t } = useTranslation()
  const fmt = useDateFormat()
  const q = useSuricataStats()
  const bySev = q.data?.by_severity ?? {}
  const high = bySev['1'] ?? 0
  const medium = bySev['2'] ?? 0
  const low = bySev['3'] ?? 0
  const total = q.data?.total_alerts ?? high + medium + low
  const series = useMemo(() => (q.data?.timeseries ?? []).map((d) => ({ ts: d.ts, count: d.count })), [q.data])

  const stat = (label: string, v: number, ink: string, to: string) => (
    <Link to={to} className="flex-1 px-2.5 py-1.5 hover:bg-[var(--panel)] min-w-0">
      <div className="text-[10px] uppercase tracking-wider text-[var(--text-dim)] truncate">{label}</div>
      <div className="font-mono tabular-nums text-lg leading-tight" style={{ color: v ? ink : 'var(--text)' }}>{v}</div>
    </Link>
  )

  return (
    <WidgetFrame icon={<ShieldAlert size={13} />} title={t('dashboard.w.ids')} editing={editing} onRemove={onRemove} bodyClassName="flex flex-col">
      {q.isPending ? (
        <Skeleton className="m-2 flex-1" />
      ) : q.isError ? (
        <div className="p-3 text-xs text-[var(--text-dim)]">{t('dashboard.idsUnavailable')}</div>
      ) : (
        <>
          <div className="flex divide-x divide-[var(--border)] border-b border-[var(--border)] shrink-0">
            {stat(t('monitoring.totalAlerts'), total, 'var(--text)', '/alerts-incidents?source=network&only_alerts=1')}
            {stat(t('monitoring.highSeverity'), high, 'var(--danger)', '/alerts-incidents?source=network&only_alerts=1&severity=1')}
            {stat(t('dashboard.sevMedium'), medium, 'var(--warn)', '/alerts-incidents?source=network&only_alerts=1&severity=2')}
          </div>
          <div className="flex-1 min-h-0 p-1">
            {series.length < 2 ? (
              <div className="h-full flex items-center justify-center text-[11px] text-[var(--text-dim)]">{t('dashboard.noIdsActivity')}</div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={series} margin={{ top: 6, right: 6, left: 6, bottom: 0 }}>
                  <XAxis dataKey="ts" hide />
                  <Tooltip {...CHART_TOOLTIP_STYLE} labelFormatter={(v) => fmt.dateTime(v as string)} formatter={(v) => [v, t('dashboard.alerts')]} />
                  <Area type="monotone" dataKey="count" stroke="var(--danger)" fill="var(--danger)" fillOpacity={0.15} strokeWidth={2} isAnimationActive={false} />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
        </>
      )}
    </WidgetFrame>
  )
}
