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

/**
 * Shared stat/metric primitives, extracted verbatim from AIAdapters.tsx so
 * the Dashboard system-health card and settings pages render the same visual
 * language as the AI metrics panels.
 */

import { ReactNode } from 'react'
import { useTranslation } from '../../i18n'

// Inline SVG sparkline — no charting dependency for a 60-point trend.
export function Sparkline({ points, height = 28, className }: {
  points: Array<number | null>
  height?: number
  className?: string
}) {
  const { t } = useTranslation()
  const vals = points.filter((v): v is number => v != null && Number.isFinite(v))
  if (vals.length < 2) {
    return <div className="text-[11px] text-[var(--text-dim)]">{t('shared.notEnoughSamples')}</div>
  }
  const min = Math.min(...vals)
  const max = Math.max(...vals)
  const span = max - min || 1
  const w = 100
  const step = w / (points.length - 1)
  let d = ''
  points.forEach((v, i) => {
    if (v == null || !Number.isFinite(v)) return
    const x = i * step
    const y = height - 3 - ((v - min) / span) * (height - 6)
    d += (d ? ' L' : 'M') + `${x.toFixed(1)} ${y.toFixed(1)}`
  })
  return (
    <svg viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none"
      className={`w-full ${className ?? ''}`} style={{ height }} aria-hidden="true">
      <path d={d} fill="none" stroke="currentColor" strokeWidth="1.5"
        vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

export function SparkRow({ label, points, latest, labelClass = 'w-14' }: { label: string; points: Array<number | null>; latest: string; labelClass?: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className={`font-mono text-[11px] text-[var(--text-dim)] shrink-0 truncate ${labelClass}`} title={label}>{label}</span>
      <div className="flex-1 text-[var(--accent,#5eb3f6)] opacity-90"><Sparkline points={points} /></div>
      <span className="font-mono text-[11px] tabular-nums w-16 text-right shrink-0">{latest}</span>
    </div>
  )
}

export function MetricPanel({ title, decision, children }: { title: string; decision: string; children: ReactNode }) {
  return (
    <div className="border border-[var(--border)] bg-[var(--bg-2)] p-3">
      <div className="text-[11px] uppercase tracking-wider text-[var(--text-dim)] mb-2 font-mono">{title}</div>
      {children}
      <div className="mt-2 text-[11px] text-[var(--text-dim)]">Decision: {decision}</div>
    </div>
  )
}

/**
 * One headline number. The single stat card for the whole app: the seven
 * per-page `Stat` copies, KpiCard and StatChip all migrate onto this.
 * `warn`/`crit` colour the figure (and add a top edge, so the state is not
 * carried by colour alone once an icon is given); `onClick` makes the whole
 * tile a button leading to where the number can be acted on.
 */
export function StatTile({ label, value, sub, warn, crit, icon, unit, onClick, children }: {
  label: ReactNode
  value: ReactNode
  sub?: ReactNode
  warn?: boolean
  crit?: boolean
  icon?: ReactNode
  /** Small suffix after the figure ("%", "/ 7"). */
  unit?: ReactNode
  onClick?: () => void
  /** Anything under the figure: a sparkline, a meter. */
  children?: ReactNode
}) {
  const valueClass = crit ? 'text-[var(--danger)]' : warn ? 'text-[var(--warn)]' : 'text-[var(--text)]'
  const edge = crit ? 'var(--danger)' : warn ? 'var(--warn)' : null
  const Comp = onClick ? 'button' : 'div'
  return (
    <Comp
      type={onClick ? 'button' : undefined}
      onClick={onClick}
      className={`block w-full text-left border border-[var(--border)] bg-[var(--bg-2)] p-3 ${onClick ? 'hover:bg-[var(--panel-2)] transition-colors' : ''}`}
      style={edge ? { boxShadow: `inset 0 2px 0 ${edge}` } : undefined}
    >
      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-[var(--text-dim)] font-mono">
        {icon && <span className="flex shrink-0">{icon}</span>}
        <span className="truncate">{label}</span>
      </div>
      <div className="flex items-baseline gap-1 mt-1">
        <span className={`font-mono text-lg font-bold tabular-nums ${valueClass}`}>{value}</span>
        {unit && <span className="font-mono text-[11px] text-[var(--text-dim)]">{unit}</span>}
      </div>
      {sub && <div className="text-[11px] text-[var(--text-dim)] mt-0.5">{sub}</div>}
      {children && <div className="mt-2">{children}</div>}
    </Comp>
  )
}

/**
 * Horizontal capacity gauge (disk usage etc). Fill goes amber past `warnAt`
 * and red past `critAt` (both fractions of `total`, e.g. 0.8 / 0.9).
 */
export function UsageBar({ used, total, warnAt = 0.8, critAt = 0.9 }: {
  used: number
  total: number
  warnAt?: number
  critAt?: number
}) {
  const frac = total > 0 ? Math.min(1, used / total) : 0
  const color = frac >= critAt ? 'bg-[var(--danger)]' : frac >= warnAt ? 'bg-[var(--warn)]' : 'bg-[var(--accent)]'
  return (
    <div className="h-2 bg-[var(--panel-2)] overflow-hidden">
      <div className={`h-full ${color}`} style={{ width: `${Math.max(2, frac * 100)}%` }} />
    </div>
  )
}
