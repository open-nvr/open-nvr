// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import type { ReactNode } from 'react'
import { GripVertical, X } from 'lucide-react'
import { clsx } from 'clsx'
import { useTranslation } from '../../i18n'
import type { CameraItem } from '../../lib/queries'

/** Class the grid uses as the drag handle — only the header drags, so
 *  buttons and scrollable lists inside the body stay usable in edit mode. */
export const DRAG_HANDLE_CLASS = 'dash-drag-handle'
/** Anything carrying this class never starts a drag, even inside the header. */
export const NO_DRAG_CLASS = 'dash-no-drag'

/**
 * The chrome every dashboard widget shares: a slim header (icon, title,
 * optional meta and actions) over a body that owns the remaining height.
 * The body is `min-h-0` so a list inside it scrolls instead of stretching
 * the grid cell.
 */
export function WidgetFrame({
  icon,
  title,
  meta,
  actions,
  editing,
  onRemove,
  children,
  bodyClassName,
}: {
  icon?: ReactNode
  title: ReactNode
  meta?: ReactNode
  actions?: ReactNode
  editing?: boolean
  onRemove?: () => void
  children: ReactNode
  bodyClassName?: string
}) {
  const { t } = useTranslation()
  return (
    <div
      className={clsx(
        'h-full flex flex-col border bg-[var(--panel-2)] overflow-hidden',
        editing ? 'border-[color-mix(in_oklab,var(--accent)_55%,var(--border))]' : 'border-[var(--border)]',
      )}
      style={{ containerType: 'inline-size' }}
    >
      <div
        className={clsx(
          DRAG_HANDLE_CLASS,
          'h-[var(--dash-head,2rem)] shrink-0 flex items-center gap-2 px-2.5 border-b border-[var(--border)] bg-[color-mix(in_oklab,var(--bg-2)_55%,var(--panel-2))]',
          editing && 'cursor-move select-none',
        )}
      >
        {editing && <GripVertical size={13} className="text-[var(--text-dim)] -ml-1" aria-hidden="true" />}
        {icon && <span className="text-[var(--text-dim)] shrink-0 flex">{icon}</span>}
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text)] truncate">{title}</h3>
        {meta && <div className="text-[11px] text-[var(--text-dim)] truncate min-w-0">{meta}</div>}
        <div className={clsx(NO_DRAG_CLASS, 'ml-auto flex items-center gap-1 shrink-0')}>
          {actions}
          {editing && onRemove && (
            <button
              type="button"
              onClick={onRemove}
              className="p-1 text-[var(--text-dim)] hover:text-[var(--danger)] hover:bg-[var(--panel)]"
              title={t('dashboard.removeWidget')}
              aria-label={t('dashboard.removeWidget')}
            >
              <X size={13} />
            </button>
          )}
        </div>
      </div>
      <div className={clsx('flex-1 min-h-0 relative', bodyClassName)}>{children}</div>
    </div>
  )
}

/** A tiny segmented selector for widget headers (ranges, tile counts). */
export function MiniSegment<T extends string | number>({
  value,
  options,
  onChange,
  label,
}: {
  value: T
  options: { value: T; label: string; title?: string }[]
  onChange: (v: T) => void
  label: string
}) {
  return (
    <div role="radiogroup" aria-label={label} className="flex border border-[var(--border)]">
      {options.map((o) => (
        <button
          key={String(o.value)}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          title={o.title}
          onClick={() => onChange(o.value)}
          className={clsx(
            'px-1.5 h-5 text-[10px] font-mono tabular-nums leading-none',
            o.value === value
              ? 'bg-[var(--accent)] text-white'
              : 'text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel)]',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

export type CamState = 'online' | 'degraded' | 'offline' | 'error'

/**
 * One camera's state for display. `live_online` null means UNKNOWN (the
 * recorder restarted and has not re-seeded, or the camera is paused), so a
 * provisioned camera with unknown liveness reads as degraded, not offline —
 * a restart must not briefly report the fleet as down.
 */
export function cameraState(c: CameraItem): CamState {
  if (c.status && ['error', 'failed'].includes(c.status)) return 'error'
  if (c.live_online === true) return 'online'
  if (c.live_online === false) return 'offline'
  if (c.status === 'provisioned' || c.status === 'active') return 'degraded'
  return 'offline'
}

export const STATE_INK: Record<CamState, string> = {
  online: 'var(--ok)',
  degraded: 'var(--warn)',
  offline: 'var(--text-dim)',
  error: 'var(--danger)',
}

export function formatBytes(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v >= 1024 ** 4) return `${(v / 1024 ** 4).toFixed(2)} TB`
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(1)} GB`
  return `${(v / 1024 ** 2).toFixed(0)} MB`
}

/** "just now" / "4m" / "3h" / "2d" — compact age for dense lists. */
export function shortAge(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return '—'
  const ms = now - new Date(iso).getTime()
  if (!Number.isFinite(ms)) return '—'
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 45) return 'now'
  const m = Math.round(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.round(m / 60)
  if (h < 48) return `${h}h`
  return `${Math.round(h / 24)}d`
}

/** Recharts tooltip styling shared by every dashboard chart. */
export const CHART_TOOLTIP_STYLE = {
  contentStyle: {
    background: 'var(--panel)',
    border: '1px solid var(--border)',
    borderRadius: 0,
    color: 'var(--text)',
    fontSize: 11,
    padding: '6px 8px',
  },
  labelStyle: { color: 'var(--text-dim)', marginBottom: 2 },
  itemStyle: { color: 'var(--text)', padding: 0 },
} as const
