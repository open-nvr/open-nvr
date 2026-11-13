// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { Film } from 'lucide-react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useTranslation, useDateFormat } from '../../../i18n'
import { useRecordingsByDate } from '../../../lib/queries'
import { formatDuration, localDateKey, localDayStart, todayLocalKey } from '../../../lib/time'
import { Skeleton } from '../../../components/ui'
import { WidgetFrame, CHART_TOOLTIP_STYLE } from '../WidgetFrame'

const DAYS = 14

/**
 * Hours of footage written per day over the last two weeks, summed across
 * cameras. A day that drops against its neighbours is a recording gap worth
 * opening — which is what the old camera-day count could never show.
 */
export function FootageWidget({ editing, onRemove }: { editing?: boolean; onRemove?: () => void }) {
  const { t } = useTranslation()
  const fmt = useDateFormat()
  const q = useRecordingsByDate()

  const data = useMemo(() => {
    const byDay = new Map<string, number>()
    for (const c of q.data?.cameras ?? []) {
      for (const r of c.recordings ?? []) {
        byDay.set(r.date, (byDay.get(r.date) ?? 0) + (r.total_duration ?? 0))
      }
    }
    // A fixed window ending today, so a day with no footage shows as an
    // empty slot rather than silently disappearing from the axis.
    const out: { day: string; hours: number }[] = []
    const end = localDayStart(todayLocalKey())
    for (let i = DAYS - 1; i >= 0; i--) {
      const d = new Date(end)
      d.setDate(d.getDate() - i)
      const key = localDateKey(d)
      out.push({ day: key, hours: Math.round(((byDay.get(key) ?? 0) / 3600) * 10) / 10 })
    }
    return out
  }, [q.data])

  const dayLabel = (key: string) => fmt.date(localDayStart(key), { day: 'numeric', month: 'short' })

  return (
    <WidgetFrame
      icon={<Film size={13} />}
      title={t('dashboard.w.footage')}
      editing={editing}
      onRemove={onRemove}
      meta={q.data?.total_duration ? <span className="font-mono tabular-nums">{formatDuration(q.data.total_duration)} {t('dashboard.stored')}</span> : undefined}
      actions={<Link to="/playback/sync" className="text-[11px] text-[var(--accent)] hover:underline px-1">{t('dashboard.playback')}</Link>}
    >
      <div className="absolute inset-0 p-1">
        {q.isPending ? (
          <Skeleton className="h-full" />
        ) : q.isError ? (
          <div className="p-2 text-xs text-[var(--danger)]">{t('dashboard.failedRecordings')}</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 10, right: 8, left: -18, bottom: 0 }} barCategoryGap={2}>
              <CartesianGrid stroke="var(--border)" strokeOpacity={0.6} vertical={false} />
              <XAxis dataKey="day" tickFormatter={dayLabel} stroke="var(--text-dim)" tick={{ fontSize: 10 }} tickLine={false} axisLine={{ stroke: 'var(--border)' }} minTickGap={16} />
              <YAxis unit="h" allowDecimals={false} stroke="var(--text-dim)" tick={{ fontSize: 10 }} tickLine={false} axisLine={false} />
              <Tooltip
                {...CHART_TOOLTIP_STYLE}
                cursor={{ fill: 'var(--text-dim)', fillOpacity: 0.08 }}
                labelFormatter={(v) => dayLabel(String(v))}
                formatter={(v) => [`${v} h`, t('dashboard.footage')]}
              />
              <Bar dataKey="hours" fill="var(--series-1)" radius={[4, 4, 0, 0]} maxBarSize={28} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </WidgetFrame>
  )
}
