// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useMemo, useState } from 'react'
import { Gauge } from 'lucide-react'
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useTranslation, useDateFormat } from '../../../i18n'
import { useSystemResources, useSystemResourcesHistory } from '../../../lib/queries'
import { Skeleton } from '../../../components/ui'
import { WidgetFrame, MiniSegment, CHART_TOOLTIP_STYLE } from '../WidgetFrame'

const RANGES = [60, 360, 1440] as const
type Range = (typeof RANGES)[number]

/**
 * CPU, memory and recordings-disk use over time, on one 0–100% axis (all
 * three are percentages, so one scale is honest). The dashed line is the
 * CPU alert threshold. History is the core's in-memory ring buffer, so it
 * starts empty after a restart.
 */
export function ResourceTrendWidget({ editing, onRemove }: { editing?: boolean; onRemove?: () => void }) {
  const { t } = useTranslation()
  const fmt = useDateFormat()
  const [range, setRange] = useState<Range>(60)
  const hist = useSystemResourcesHistory(range)
  const res = useSystemResources()
  const cpuThr = res.data?.thresholds?.cpu_percent_threshold
  const temps = res.data?.temperatures ?? []
  const hottest = temps.length ? Math.max(...temps.map((x) => x.celsius)) : null

  const data = useMemo(
    () => (hist.data ?? []).map((p) => ({
      ts: p.ts * 1000,
      cpu: p.cpu_percent != null ? Math.round(p.cpu_percent * 10) / 10 : null,
      mem: p.memory_percent != null ? Math.round(p.memory_percent * 10) / 10 : null,
      disk: p.disk_percent != null ? Math.round(p.disk_percent * 10) / 10 : null,
      npu: p.npu_percent != null ? Math.round(p.npu_percent * 10) / 10 : null,
      gpu: p.gpu_percent != null ? Math.round(p.gpu_percent * 10) / 10 : null,
    })),
    [hist.data],
  )
  const last = data[data.length - 1]

  // The NPU and GPU lines appear only on a box that has one in use: a share
  // of the device's time spent computing, from the inference time the
  // detector and the adapters report (no kernel counter on most boards).
  const hasNpu = (hist.data ?? []).some((p) => p.npu_percent != null)
  const hasGpu = (hist.data ?? []).some((p) => p.gpu_percent != null)
  const series: { key: 'cpu' | 'mem' | 'disk' | 'npu' | 'gpu'; label: string; ink: string }[] = [
    { key: 'cpu', label: 'CPU', ink: 'var(--series-1)' },
    { key: 'mem', label: t('dashboard.memory'), ink: 'var(--series-2)' },
    { key: 'disk', label: t('dashboard.disk'), ink: 'var(--series-3)' },
    ...(hasNpu ? [{ key: 'npu' as const, label: 'NPU', ink: 'var(--series-4)' }] : []),
    ...(hasGpu ? [{ key: 'gpu' as const, label: 'GPU', ink: 'var(--series-5)' }] : []),
  ] as const

  const tick = (v: number) =>
    range <= 360 ? fmt.time(v, { hour: '2-digit', minute: '2-digit' }) : fmt.time(v, { hour: '2-digit' })

  return (
    <WidgetFrame
      icon={<Gauge size={13} />}
      title={t('dashboard.w.resources')}
      editing={editing}
      onRemove={onRemove}
      actions={
        <MiniSegment
          label={t('dashboard.timeRange')}
          value={range}
          onChange={setRange}
          options={[
            { value: 60, label: '1h' },
            { value: 360, label: '6h' },
            { value: 1440, label: '24h' },
          ]}
        />
      }
      bodyClassName="flex flex-col"
    >
      {/* Legend doubles as the live readout. */}
      <div className="flex items-center gap-3 px-2.5 pt-1.5 text-[10.5px] shrink-0">
        {series.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1.5 text-[var(--text-dim)]">
            <span className="w-2.5 h-[2px]" style={{ background: s.ink }} aria-hidden="true" />
            {s.label}
            <span className="font-mono tabular-nums text-[var(--text)]">{last?.[s.key] != null ? `${Math.round(last[s.key] as number)}%` : '—'}</span>
          </span>
        ))}
        {/* Who measured: the host agent sees the machine; without it, core
            sees its container — on Windows/macOS that is Docker's VM. */}
        {res.data?.measured_by && (
          <span
            className="ml-auto inline-flex items-center gap-1.5 text-[var(--text-dim)]"
            title={res.data.measured_by === 'host-agent'
              ? 'Measured on the OpenNVR machine by the host agent'
              : 'Measured inside OpenNVR’s container. On Windows and macOS this is Docker’s VM, not the whole machine; install the host agent for the machine’s own numbers.'}
          >
            {hottest != null && <span className="font-mono tabular-nums text-[var(--text)]">{Math.round(hottest)}°C</span>}
            {res.data.measured_by === 'host-agent' ? 'host' : 'container'}
          </span>
        )}
      </div>
      <div className="flex-1 min-h-0 px-1 pb-1">
        {hist.isPending ? (
          <Skeleton className="h-full m-1" />
        ) : data.length < 2 ? (
          <div className="h-full flex items-center justify-center text-[11px] text-[var(--text-dim)]">{t('dashboard.collectingSamples')}</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
              <CartesianGrid stroke="var(--border)" strokeOpacity={0.6} vertical={false} />
              <XAxis
                dataKey="ts"
                type="number"
                scale="time"
                domain={['dataMin', 'dataMax']}
                tickFormatter={tick}
                stroke="var(--text-dim)"
                tick={{ fontSize: 10 }}
                tickLine={false}
                axisLine={{ stroke: 'var(--border)' }}
                minTickGap={40}
              />
              <YAxis domain={[0, 100]} ticks={[0, 50, 100]} unit="%" stroke="var(--text-dim)" tick={{ fontSize: 10 }} tickLine={false} axisLine={false} />
              {cpuThr != null && <ReferenceLine y={cpuThr} stroke="var(--text-dim)" strokeDasharray="3 3" strokeOpacity={0.7} />}
              <Tooltip
                {...CHART_TOOLTIP_STYLE}
                cursor={{ stroke: 'var(--text-dim)', strokeWidth: 1 }}
                labelFormatter={(v) => fmt.dateTime(Number(v))}
                formatter={(v, name) => [`${v}%`, series.find((s) => s.key === name)?.label ?? name]}
              />
              {series.map((s) => (
                <Line key={s.key} dataKey={s.key} name={s.key} stroke={s.ink} strokeWidth={2} dot={false} isAnimationActive={false} connectNulls={false} />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </WidgetFrame>
  )
}
