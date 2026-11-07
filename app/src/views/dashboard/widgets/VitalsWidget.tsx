// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useMemo, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Activity, Bell, Camera, Cpu, HardDrive, MemoryStick, Video } from 'lucide-react'
import { clsx } from 'clsx'
import { useTranslation } from '../../../i18n'
import {
  useCameras,
  useSystemResources,
  useSystemResourcesHistory,
  useUnackedAlerts,
} from '../../../lib/queries'
import { Sparkline } from '../../../components/ui/stats'
import { WidgetFrame, cameraState, formatBytes, type CamState, STATE_INK } from '../WidgetFrame'

type Tone = 'ok' | 'warn' | 'crit' | 'neutral'
const TONE_INK: Record<Tone, string> = {
  ok: 'var(--text)',
  neutral: 'var(--text)',
  warn: 'var(--warn)',
  crit: 'var(--danger)',
}

function Tile({
  icon,
  label,
  value,
  unit,
  sub,
  tone = 'neutral',
  onClick,
  children,
}: {
  icon: ReactNode
  label: string
  value: ReactNode
  unit?: string
  sub?: ReactNode
  tone?: Tone
  onClick?: () => void
  children?: ReactNode
}) {
  const Comp = onClick ? 'button' : 'div'
  return (
    <Comp
      type={onClick ? 'button' : undefined}
      onClick={onClick}
      className={clsx(
        'vital-tile relative flex flex-col min-w-0 min-h-0 text-left px-3 py-1.5 bg-[var(--panel-2)] overflow-hidden',
        onClick && 'hover:bg-[color-mix(in_oklab,var(--panel)_60%,var(--panel-2))] transition-colors',
      )}
      style={tone === 'warn' || tone === 'crit' ? { boxShadow: `inset 0 2px 0 ${TONE_INK[tone]}` } : undefined}
    >
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.12em] text-[var(--text-dim)]">
        <span className="shrink-0 flex">{icon}</span>
        <span className="truncate">{label}</span>
      </div>
      <div className="flex items-baseline gap-1 mt-0.5">
        <span className="vital-value font-mono tabular-nums text-[22px] @3xl:text-[26px] leading-none font-semibold" style={{ color: TONE_INK[tone] }}>
          {value}
        </span>
        {unit && <span className="font-mono text-[11px] text-[var(--text-dim)]">{unit}</span>}
      </div>
      {sub && <div className="vital-sub text-[10.5px] text-[var(--text-dim)] mt-1 truncate">{sub}</div>}
      {children && <div className="vital-extra mt-auto pt-1 min-h-0">{children}</div>}
    </Comp>
  )
}

/** Stacked segment bar: one fill per state, a 2px gap between fills. */
function SegmentBar({ parts }: { parts: { key: string; value: number; ink: string; label: string }[] }) {
  const total = parts.reduce((s, p) => s + p.value, 0)
  if (!total) return <div className="h-1.5 bg-[var(--bg-2)]" />
  return (
    <div className="flex h-1.5 gap-[2px]" role="img" aria-label={parts.map((p) => `${p.value} ${p.label}`).join(', ')}>
      {parts.filter((p) => p.value > 0).map((p) => (
        <div key={p.key} title={`${p.value} ${p.label}`} style={{ flexGrow: p.value, background: p.ink }} />
      ))}
    </div>
  )
}

function Meter({ percent, warnAt, critAt = 98 }: { percent: number | null | undefined; warnAt?: number | null; critAt?: number }) {
  const p = percent == null || !Number.isFinite(percent) ? 0 : Math.max(0, Math.min(100, percent))
  const ink = p >= critAt ? 'var(--danger)' : warnAt != null && p >= warnAt ? 'var(--warn)' : 'var(--accent)'
  return (
    <div className="relative h-1.5 bg-[var(--bg-2)]">
      <div className="absolute inset-y-0 left-0" style={{ width: `${Math.max(1.5, p)}%`, background: ink }} />
      {warnAt != null && (
        <div className="absolute -top-0.5 -bottom-0.5 w-px bg-[var(--text-dim)] opacity-70" style={{ left: `${warnAt}%` }} title={`${warnAt}%`} />
      )}
    </div>
  )
}

/**
 * The headline numbers, one tile each, sized to read from across a room:
 * camera fleet, recording, CPU, memory, storage, open alarms. Every tile
 * leads to the page where the operator can act on it.
 */
export function VitalsWidget({ editing, onRemove }: { editing?: boolean; onRemove?: () => void }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const cams = useCameras()
  const res = useSystemResources()
  const hist = useSystemResourcesHistory(60)
  const alarms = useUnackedAlerts()

  const list = cams.data?.cameras ?? []
  const byState = useMemo(() => {
    const agg: Record<CamState, number> = { online: 0, degraded: 0, offline: 0, error: 0 }
    for (const c of list) agg[cameraState(c)]++
    return agg
  }, [list])
  const recording = list.filter((c) => c.recording_state === 'recording').length
  const stalled = list.filter((c) => c.recording_state === 'stalled').length
  const recOff = list.length - recording - stalled

  const cpu = res.data?.cpu_percent
  const mem = res.data?.memory
  const disk = res.data?.disk
  const thr = res.data?.thresholds
  const cpuPts = (hist.data ?? []).map((p) => p.cpu_percent)
  const accel = res.data?.accelerators ?? []
  const npu = accel.find((a) => a.kind === 'npu')
  const npuPts = (hist.data ?? []).map((p) => p.npu_percent ?? null)
  const memPts = (hist.data ?? []).map((p) => p.memory_percent)

  const alarmList = alarms.data?.alerts ?? []
  const critical = alarmList.filter((a) => a.severity === 'critical').length
  const high = alarmList.filter((a) => a.severity === 'high').length
  const unacked = alarms.data?.unacked_count ?? 0

  const pct = (v: number | null | undefined) => (v == null || !Number.isFinite(v) ? '—' : String(Math.round(v)))
  const level = (v: number | null | undefined, warn?: number | null): Tone =>
    v == null ? 'neutral' : v >= 95 ? 'crit' : warn != null && v >= warn ? 'warn' : 'neutral'

  return (
    <WidgetFrame icon={<Activity size={13} />} title={t('dashboard.w.vitals')} editing={editing} onRemove={onRemove}>
      <div className="absolute inset-0 grid grid-cols-3 @4xl:grid-cols-6 auto-rows-fr gap-px bg-[var(--border)]">
        <Tile
          icon={<Camera size={12} />}
          label={t('dashboard.camerasOnline')}
          value={cams.data ? byState.online : '—'}
          unit={cams.data ? `/ ${list.length}` : undefined}
          tone={byState.error || byState.offline ? 'warn' : 'neutral'}
          sub={cams.data
            ? [
                byState.degraded ? `${byState.degraded} ${t('dashboard.statusDegraded').toLowerCase()}` : null,
                byState.offline ? `${byState.offline} ${t('dashboard.statusOffline').toLowerCase()}` : null,
                byState.error ? `${byState.error} ${t('dashboard.statusError').toLowerCase()}` : null,
              ].filter(Boolean).join(' · ') || t('dashboard.allOnline')
            : undefined}
          onClick={() => navigate('/cameras')}
        >
          <SegmentBar parts={[
            { key: 'online', value: byState.online, ink: STATE_INK.online, label: t('dashboard.statusOnline') },
            { key: 'degraded', value: byState.degraded, ink: STATE_INK.degraded, label: t('dashboard.statusDegraded') },
            { key: 'offline', value: byState.offline, ink: STATE_INK.offline, label: t('dashboard.statusOffline') },
            { key: 'error', value: byState.error, ink: STATE_INK.error, label: t('dashboard.statusError') },
          ]} />
        </Tile>

        <Tile
          icon={<Video size={12} />}
          label={t('dashboard.recordingNow')}
          value={cams.data ? recording : '—'}
          unit={cams.data ? `/ ${list.length}` : undefined}
          tone={stalled ? 'warn' : 'neutral'}
          sub={stalled ? `${stalled} ${t('dashboard.stalled')}` : recOff ? `${recOff} ${t('dashboard.notRecording')}` : t('dashboard.allRecording')}
          onClick={() => navigate('/playback/sync')}
        >
          <SegmentBar parts={[
            { key: 'rec', value: recording, ink: 'var(--ok)', label: t('dashboard.recordingNow') },
            { key: 'stalled', value: stalled, ink: 'var(--warn)', label: t('dashboard.stalled') },
            { key: 'off', value: recOff, ink: 'var(--text-dim)', label: t('dashboard.notRecording') },
          ]} />
        </Tile>

        <Tile
          icon={<Cpu size={12} />}
          label="CPU"
          value={pct(cpu)}
          unit="%"
          tone={level(cpu, thr?.cpu_percent_threshold)}
          sub={res.data?.load_avg ? `${t('dashboard.load')} ${res.data.load_avg.map((v) => v.toFixed(1)).join(' ')}` : res.data?.monitoring_available === false ? t('dashboard.monitoringOff') : undefined}
        >
          <div className="text-[var(--accent)]"><Sparkline points={cpuPts} height={16} /></div>
        </Tile>

        {npu && (
          <Tile
            icon={<Cpu size={12} />}
            label="NPU"
            value={pct(npu.busy_percent)}
            unit="%"
            tone={level(npu.busy_percent, 90)}
            sub={npu.users.length ? npu.users.map((u) => `${u.name} ${Math.round(u.percent)}%`).join(' · ') : npu.name}
          >
            <div className="text-[var(--accent)]"><Sparkline points={npuPts} height={16} /></div>
          </Tile>
        )}

        <Tile
          icon={<MemoryStick size={12} />}
          label={t('dashboard.memory')}
          value={pct(mem?.percent)}
          unit="%"
          tone={level(mem?.percent, thr?.memory_percent_threshold)}
          sub={mem ? `${formatBytes(mem.used)} / ${formatBytes(mem.total)}` : undefined}
        >
          <div className="text-[var(--accent)]"><Sparkline points={memPts} height={16} /></div>
        </Tile>

        <Tile
          icon={<HardDrive size={12} />}
          label={t('dashboard.recordingsDisk')}
          value={disk ? formatBytes(disk.free).split(' ')[0] : '—'}
          unit={disk ? `${formatBytes(disk.free).split(' ')[1]} ${t('dashboard.free')}` : undefined}
          tone={disk ? (disk.percent >= 98 ? 'crit' : thr?.disk_used_percent_threshold != null && disk.percent >= thr.disk_used_percent_threshold ? 'warn' : 'neutral') : res.data?.disk_error ? 'crit' : 'neutral'}
          sub={disk ? `${Math.round(disk.percent)}% ${t('dashboard.used')} ${t('dashboard.of')} ${formatBytes(disk.total)}` : res.data?.disk_error ?? undefined}
        >
          <Meter percent={disk?.percent} warnAt={thr?.disk_used_percent_threshold} />
        </Tile>

        <Tile
          icon={<Bell size={12} />}
          label={t('dashboard.openAlarms')}
          value={alarms.data ? unacked : '—'}
          tone={critical ? 'crit' : high ? 'warn' : 'neutral'}
          sub={alarms.data
            ? (critical || high
              ? [critical ? `${critical} ${t('dashboard.sevCritical')}` : null, high ? `${high} ${t('dashboard.sevHigh')}` : null].filter(Boolean).join(' · ')
              : unacked ? t('dashboard.lowSeverityOnly') : t('dashboard.allClear'))
            : undefined}
          onClick={() => navigate('/alerts-incidents')}
        />
      </div>
    </WidgetFrame>
  )
}
