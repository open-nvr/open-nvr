// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { clsx } from 'clsx'
import { useTranslation } from '../../i18n'
import { useCameraStatusConnected } from '../../hooks/useCameraStatus'
import {
  useCameras,
  useCoreHealth,
  useKaiCHealth,
  useMediaMtxHealth,
  useSystemResources,
  useTier0Health,
} from '../../lib/queries'
import { formatBytes } from './WidgetFrame'

type Level = 'ok' | 'warn' | 'crit' | 'unknown'

const LEVEL_INK: Record<Level, string> = {
  ok: 'var(--ok)',
  warn: 'var(--warn)',
  crit: 'var(--danger)',
  unknown: 'var(--text-dim)',
}

function Dot({ level, pulse }: { level: Level; pulse?: boolean }) {
  return (
    <span className="relative inline-flex w-2 h-2 shrink-0" aria-hidden="true">
      {pulse && (
        <span className="absolute inset-0 rounded-full animate-ping opacity-60" style={{ background: LEVEL_INK[level] }} />
      )}
      <span className="relative inline-block w-2 h-2 rounded-full" style={{ background: LEVEL_INK[level] }} />
    </span>
  )
}

function Chip({ level, label, value, title, to }: { level: Level; label: string; value: string; title?: string; to?: string }) {
  const body = (
    <>
      <Dot level={level} />
      <span className="text-[var(--text-dim)] uppercase tracking-wider text-[10px]">{label}</span>
      <span
        className="font-mono tabular-nums text-[11px]"
        style={{ color: level === 'ok' || level === 'unknown' ? 'var(--text)' : LEVEL_INK[level] }}
      >
        {value}
      </span>
    </>
  )
  const cls = 'flex items-center gap-1.5 px-2.5 h-full border-l border-[var(--border)] whitespace-nowrap'
  return to ? (
    <Link to={to} title={title} className={clsx(cls, 'hover:bg-[var(--panel)]')}>{body}</Link>
  ) : (
    <div title={title} className={cls}>{body}</div>
  )
}

/** The accelerator a provider list names, in the word an operator uses. */
function acceleratorWord(effective: string): string {
  const first = effective.split(',')[0].trim()
  if (first.startsWith('QNN')) return 'NPU'
  if (first.startsWith('OpenVINO')) return /NPU/.test(effective) ? 'NPU' : 'iGPU'
  if (first.startsWith('CUDA') || first.startsWith('Tensorrt') || first.startsWith('TensorRT')) return 'GPU'
  if (first.startsWith('CPU') || !first) return 'CPU'
  return first.replace('ExecutionProvider', '')
}

function formatUptime(s: number): string {
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`
}

/**
 * The line of service health that sits above the widget grid and never
 * moves: whether the core, the media server and the AI engine answer,
 * whether live status is flowing, and how recording and storage are doing.
 * Pinned rather than a widget because it is the one thing an operator must
 * see first, whatever they have done to the rest of the layout.
 */
export function HealthBar({ actions }: { actions?: ReactNode }) {
  const { t } = useTranslation()
  const core = useCoreHealth()
  const mtx = useMediaMtxHealth()
  const kaic = useKaiCHealth()
  const cams = useCameras()
  const res = useSystemResources()
  const tier0 = useTier0Health()
  const socketUp = useCameraStatusConnected()

  const coreLevel: Level = core.isError ? 'crit' : core.data ? 'ok' : 'unknown'
  const mtxLevel: Level = mtx.isError || (mtx.data && mtx.data.status !== 'ok') ? 'crit' : mtx.data ? 'ok' : 'unknown'
  // The AI engine is optional to recording and live view, so its outage is
  // a warning, and it does not pull the overall verdict down on its own.
  const aiLevel: Level = kaic.isError || kaic.data?.kai_c_status === 'error' ? 'warn' : kaic.data ? 'ok' : 'unknown'
  const eventsLevel: Level = socketUp ? 'ok' : 'warn'

  const list = cams.data?.cameras ?? []
  const recording = list.filter((c) => c.recording_state === 'recording').length
  const stalled = list.filter((c) => c.recording_state === 'stalled' || c.recording_state === 'degraded').length
  const recLevel: Level = !cams.data ? 'unknown' : stalled > 0 ? 'warn' : 'ok'

  const disk = res.data?.disk
  const diskThr = res.data?.thresholds?.disk_used_percent_threshold
  const diskLevel: Level = !disk
    ? (res.data?.disk_error ? 'crit' : 'unknown')
    : disk.percent >= 98 ? 'crit'
      : diskThr != null && disk.percent >= diskThr ? 'warn' : 'ok'
  const resourceAlerts = res.data?.active_alerts?.length ?? 0
  // Disk rotation: core is deleting the oldest recordings to keep its
  // headroom. Recording continues; the operator should know what the
  // effective retention has become, or that the floor cannot be reached
  // because the disk is full of other data.
  const rotation = res.data?.rotation ?? null
  const rotationText = rotation
    ? rotation.unreachable
      ? t('dashboard.health.rotationUnreachable', { gb: rotation.min_free_gb ?? '' })
      : rotation.effective_retention_hours != null
        ? t('dashboard.health.rotating', { hours: Math.round(rotation.effective_retention_hours) })
        : t('dashboard.health.rotatingShort')
    : null

  // The detect-pipeline: a stack whose detector is down, idle, or on CPU
  // after asking for an accelerator looked healthy everywhere on this bar
  // for an hour (Ventuno Q, 2026-11-01). It is its own chip, and a dead
  // pipeline is an outage: every alarm, app and search rides on it.
  const t0 = tier0.data
  const t0Backend = t0?.backend ?? null
  const detLevel: Level = tier0.isError
    ? 'warn'
    : !t0 ? 'unknown'
      : !t0.available ? (t0.reason === 'disabled' ? 'unknown' : 'crit')
        : t0.mode === 'not_running' || t0Backend?.fallback ? 'warn'
          : (t0.health && t0.health.workers_total > 0 && t0.health.workers_up < t0.health.workers_total) ? 'warn' : 'ok'
  const detValue =
    !t0 ? '…'
      : !t0.available ? (t0.reason === 'disabled' ? t('dashboard.health.off') : t('dashboard.health.down'))
        : t0Backend?.fallback ? t('dashboard.health.cpuFallback')
          : t0.mode === 'not_running' ? t('dashboard.health.noFrames')
            : [t0.health ? `${t0.health.workers_up}/${t0.health.workers_total}` : '', t0Backend ? acceleratorWord(t0Backend.effective) : '']
              .filter(Boolean).join(' · ')
  const detTitle = t0Backend
    ? `${t0Backend.requested ? `${t0Backend.requested} requested · ` : ''}${t0Backend.effective || 'CPUExecutionProvider'} in use`
    : t0?.reason === 'unreachable' ? t('dashboard.health.detectionDownHint') : undefined

  const problems: string[] = []
  if (coreLevel === 'crit') problems.push(t('dashboard.health.coreApi'))
  if (mtxLevel === 'crit') problems.push(t('dashboard.health.mediaServer'))
  if (diskLevel === 'crit') problems.push(t('dashboard.health.storage'))
  if (detLevel === 'crit') problems.push(t('dashboard.health.detection'))
  const warnings: string[] = []
  if (detLevel === 'warn') warnings.push(`${t('dashboard.health.detection')}: ${detValue}`)
  if (stalled > 0) warnings.push(t('dashboard.health.recording'))
  if (diskLevel === 'warn') warnings.push(t('dashboard.health.storage'))
  if (rotationText && diskLevel !== 'crit') warnings.push(rotationText)
  if (resourceAlerts > 0) warnings.push(t('dashboard.health.resources'))
  if (!socketUp) warnings.push(t('dashboard.health.liveEvents'))

  const overall: Level = problems.length ? 'crit' : warnings.length ? 'warn' : coreLevel === 'unknown' ? 'unknown' : 'ok'
  const verdict =
    overall === 'crit' ? t('dashboard.health.outage')
      : overall === 'warn' ? t('dashboard.health.degraded')
        : overall === 'ok' ? t('dashboard.health.operational')
          : t('dashboard.health.checking')
  const detail = (problems.length ? problems : warnings).join(' · ')

  return (
    // Not overflow-hidden: the edit-mode "Add widget" menu drops out of this
    // bar, and clipping here swallowed it.
    <div className="h-11 shrink-0 flex items-stretch border border-[var(--border)] bg-[var(--panel-2)] relative z-20">
      <div
        className="flex items-center gap-2.5 pl-3 pr-4 min-w-0 shrink-0"
        style={{ boxShadow: `inset 3px 0 0 ${LEVEL_INK[overall]}` }}
        role="status"
        aria-live="polite"
      >
        <Dot level={overall} pulse={overall === 'crit'} />
        <div className="leading-tight min-w-0">
          <div className="text-[13px] font-semibold text-[var(--text)] whitespace-nowrap">{verdict}</div>
          <div className="text-[10px] text-[var(--text-dim)] truncate max-w-[18rem]">
            {detail || t('dashboard.health.allServices')}
          </div>
        </div>
      </div>

      <div className="flex items-stretch min-w-0 overflow-x-auto no-scrollbar">
        <Chip
          level={coreLevel}
          label={t('dashboard.health.coreApi')}
          value={coreLevel === 'crit' ? t('dashboard.health.unreachable') : core.data?.version ? `v${core.data.version}` : '…'}
          title={core.data?.uptime_s != null ? `${t('dashboard.health.uptime')} ${formatUptime(core.data.uptime_s)}` : undefined}
        />
        <Chip
          level={mtxLevel}
          label={t('dashboard.health.mediaServer')}
          value={mtxLevel === 'crit' ? t('dashboard.health.down') : mtxLevel === 'ok' ? t('dashboard.health.up') : '…'}
          to="/settings"
        />
        <Chip
          level={aiLevel}
          label={t('dashboard.health.aiEngine')}
          value={aiLevel === 'warn' ? t('dashboard.health.unavailable') : aiLevel === 'ok' ? t('dashboard.health.up') : '…'}
          title={kaic.data?.message ?? undefined}
          to="/ai-adapters"
        />
        <Chip
          level={detLevel}
          label={t('dashboard.health.detection')}
          value={detValue}
          title={detTitle}
          to="/ai-adapters"
        />
        <Chip
          level={eventsLevel}
          label={t('dashboard.health.liveEvents')}
          value={socketUp ? t('dashboard.health.streaming') : t('dashboard.health.reconnecting')}
          title={socketUp ? undefined : t('dashboard.reconnectingHint')}
        />
        <Chip
          level={recLevel}
          label={t('dashboard.health.recording')}
          value={cams.data ? `${recording}/${list.length}${stalled ? ` · ${stalled} ${t('dashboard.stalled')}` : ''}` : '…'}
          to="/cameras"
        />
        <Chip
          level={rotation && diskLevel === 'ok' ? 'warn' : diskLevel}
          label={t('dashboard.health.storage')}
          value={disk ? `${formatBytes(disk.free)} ${t('dashboard.free')}${rotation ? ` · ${t('dashboard.health.rotatingShort')}` : ''}` : diskLevel === 'crit' ? t('dashboard.health.error') : '…'}
          title={rotation?.description ?? (disk ? `${disk.path} · ${Math.round(disk.percent)}% ${t('dashboard.used')}` : res.data?.disk_error ?? undefined)}
        />
      </div>

      <div className="ml-auto flex items-stretch shrink-0">
        {actions && <div className="flex items-center gap-1.5 px-2 border-l border-[var(--border)]">{actions}</div>}
      </div>
    </div>
  )
}
