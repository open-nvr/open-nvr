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

// Shared UI primitives. All views should compose these instead of redefining
// per-view cards/badges/buttons, so theming stays consistent across apps.
// Colors come exclusively from the CSS variable tokens in index.css.

import { clsx } from 'clsx'
import { CircleAlert, Inbox, RefreshCw } from 'lucide-react'
import type { ReactNode, ButtonHTMLAttributes, TableHTMLAttributes, HTMLAttributes, ThHTMLAttributes, TdHTMLAttributes } from 'react'

/* ----------------------------- Card ----------------------------- */

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={clsx('rounded border border-[var(--border)] bg-[var(--panel-2)]', className)}>{children}</div>
}

export function CardHeader({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={clsx('px-4 py-3 border-b border-[var(--border)] flex items-center gap-2', className)}>{children}</div>
}

export function CardTitle({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <h3 className={clsx('text-sm font-semibold text-[var(--text)] tracking-wide', className)}>{children}</h3>
}

export function CardContent({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={clsx('p-4', className)}>{children}</div>
}

/* ----------------------------- Badge ---------------------------- */

export type BadgeVariant = 'success' | 'warning' | 'destructive' | 'neutral' | 'info' | 'critical'

// Theme-token driven (see the --badge-* variables in index.css): the dark
// theme keeps the translucent deep tints, the light theme swaps to pale
// tints with dark text — raw palette classes here would wash out on light.
const BADGE_STYLES: Record<BadgeVariant, string> = {
  success: 'bg-[var(--badge-success-bg)] text-[var(--badge-success-text)]',
  warning: 'bg-[var(--badge-warning-bg)] text-[var(--badge-warning-text)]',
  destructive: 'bg-[var(--badge-destructive-bg)] text-[var(--badge-destructive-text)]',
  neutral: 'bg-[var(--badge-neutral-bg)] text-[var(--badge-neutral-text)]',
  info: 'bg-[var(--badge-info-bg)] text-[var(--badge-info-text)]',
  critical: 'bg-[var(--badge-critical-bg)] text-[var(--badge-critical-text)]',
}

/* ------------------------- SeverityBadge ------------------------ */

// One severity pill for every alarm surface. Both alarm tables used to
// carry their own identical Record<string,string> of raw palette classes
// (`bg-red-600 text-white`, ...), which is the rule this file states at
// the top not to do — and it showed: `bg-yellow-600 text-black` on the
// light theme was close to unreadable. Four severities, four tokens.
const SEVERITY_VARIANT: Record<string, BadgeVariant> = {
  critical: 'critical',
  high: 'destructive',
  medium: 'warning',
  low: 'neutral',
}

export function SeverityBadge({ severity, className = '' }: { severity?: string | null; className?: string }) {
  const key = (severity ?? '').toLowerCase()
  return (
    <Badge
      variant={SEVERITY_VARIANT[key] ?? 'neutral'}
      className={clsx('uppercase text-[10px] tracking-wide', className)}
    >
      {severity || 'unknown'}
    </Badge>
  )
}

// Spreads span attributes so callers can attach a `title` — a badge that
// summarises a state often needs to explain it on hover.
export function Badge({ children, variant = 'neutral', className = '', ...rest }: HTMLAttributes<HTMLSpanElement> & { variant?: BadgeVariant }) {
  return <span className={clsx('inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px]', BADGE_STYLES[variant], className)} {...rest}>{children}</span>
}

/* ---------------------------- Button ---------------------------- */

export type ButtonVariant = 'default' | 'primary' | 'outline' | 'ghost' | 'danger'

const BUTTON_STYLES: Record<ButtonVariant, string> = {
  default: 'border border-[var(--border)] bg-[var(--panel-2)] hover:bg-[var(--panel)] text-[var(--text)]',
  primary: 'border border-transparent bg-[var(--accent)] text-white hover:brightness-95',
  outline: 'border border-[var(--border)] bg-transparent hover:bg-[var(--panel-2)] text-[var(--text)]',
  ghost: 'border border-transparent bg-transparent hover:bg-[var(--panel-2)] text-[var(--text-dim)] hover:text-[var(--text)]',
  danger: 'border border-red-700/50 bg-red-900/30 text-red-300 hover:bg-red-900/50',
}

// Two sizes, because a page header full of default-size buttons competes
// with the page title for weight. `sm` is for toolbars and header
// actions; `md` stays the default everywhere else. A size PROP rather
// than a className override: Tailwind utilities have equal specificity,
// so passing `px-2` alongside the base `px-3` wins or loses on
// stylesheet order, not on intent.
export type ButtonSize = 'sm' | 'md'

const BUTTON_SIZES: Record<ButtonSize, string> = {
  sm: 'gap-1.5 px-2 py-1 text-xs',
  md: 'gap-2 px-3 py-1.5 text-sm',
}

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant
  size?: ButtonSize
}

export function Button({ children, variant = 'default', size = 'md', className = '', type = 'button', ...rest }: ButtonProps) {
  return (
    <button
      type={type}
      className={clsx('inline-flex items-center rounded disabled:opacity-50 disabled:cursor-not-allowed', BUTTON_SIZES[size], BUTTON_STYLES[variant], className)}
      {...rest}
    >
      {children}
    </button>
  )
}

/* --------------------------- Skeleton --------------------------- */

export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={clsx('animate-pulse rounded-md bg-[var(--bg-2)]', className)} />
}

/* --------------------------- StatusDot -------------------------- */

export type Status = 'online' | 'offline' | 'degraded' | 'error'

const STATUS_STYLES: Record<Status, string> = {
  online: 'bg-emerald-500',
  offline: 'bg-slate-500',
  degraded: 'bg-amber-500',
  error: 'bg-red-500',
}

export function StatusDot({ status }: { status: Status }) {
  return <span className={clsx('inline-block w-2 h-2 rounded-full', STATUS_STYLES[status])} />
}

/* -------------------------- PageHeader -------------------------- */

// The text column must SHRINK, not push. Without `min-w-0 flex-1` its
// hypothetical size is max-content — a long description laid out on one
// unbroken line — and flexbox wraps a line before it shrinks an item, so
// any page with a sentence-length description shoved its actions onto a
// second flex row. `justify-between` then parked that lone item at the
// START, which is why several pages render their buttons bottom-LEFT
// under the description instead of top-right. AIAdapters, AppCatalog and
// Vehicles all had it; Occupancy was one button away from it.
//
// `ml-auto` (not `justify-between`) is what right-aligns the actions,
// and it keeps working on the wrapped line at narrow widths. `shrink-0`
// stops the buttons compressing into two-line labels before the row
// wraps. `max-w-3xl` keeps a long description readable rather than
// running the full width of a 2560px monitor.
export function PageHeader({ title, description, actions }: { title: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start gap-3 mb-4">
      <div className="min-w-0 flex-1">
        <h2 className="text-lg font-semibold text-[var(--text)]">{title}</h2>
        {description && <p className="text-sm text-[var(--text-dim)] mt-0.5 max-w-3xl">{description}</p>}
      </div>
      {actions && <div className="ml-auto shrink-0 flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

/* -------------------------- EmptyState -------------------------- */

export function EmptyState({ icon, title, description, action }: { icon?: ReactNode; title: ReactNode; description?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-12 px-4">
      <div className="text-[var(--text-dim)] mb-3">{icon ?? <Inbox size={28} />}</div>
      <div className="text-sm font-medium text-[var(--text)]">{title}</div>
      {description && <div className="text-sm text-[var(--text-dim)] mt-1 max-w-md">{description}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

/* -------------------------- ErrorCard --------------------------- */

export function ErrorCard({ title = 'Error', message, onRetry }: { title?: string; message: string; onRetry?: () => void }) {
  return (
    <Card className="border-red-700/40">
      <CardHeader>
        <CircleAlert size={16} className="text-red-300" />
        <CardTitle>{title}</CardTitle>
        {onRetry && (
          <div className="ml-auto">
            <Button onClick={onRetry}>
              <RefreshCw size={14} /> Retry
            </Button>
          </div>
        )}
      </CardHeader>
      <CardContent>
        <div className="text-sm text-red-300/90">{message}</div>
      </CardContent>
    </Card>
  )
}

/* ---------------------------- Table ----------------------------- */

export function Table({ children, className = '', ...rest }: TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="overflow-x-auto border border-[var(--border)] rounded">
      <table className={clsx('w-full text-sm', className)} {...rest}>
        {children}
      </table>
    </div>
  )
}

export function THead({ children, className = '', ...rest }: HTMLAttributes<HTMLTableSectionElement>) {
  return (
    <thead className={clsx('bg-[var(--bg-2)] text-[var(--text-dim)] text-left', className)} {...rest}>
      {children}
    </thead>
  )
}

export function TBody({ children, striped = false, className = '', ...rest }: HTMLAttributes<HTMLTableSectionElement> & { striped?: boolean }) {
  return (
    <tbody
      className={clsx(
        'divide-y divide-[var(--border)]',
        // Zebra rows for dense tables; hover stays dominant via !important.
        striped && '[&>tr:nth-child(odd)]:bg-[var(--bg-2)] [&>tr:hover]:!bg-[var(--panel-2)]',
        className
      )}
      {...rest}
    >
      {children}
    </tbody>
  )
}

export function TR({ children, className = '', ...rest }: HTMLAttributes<HTMLTableRowElement>) {
  return (
    <tr className={clsx('hover:bg-[var(--panel-2)]', className)} {...rest}>
      {children}
    </tr>
  )
}

// scope="col" is defaulted rather than left to callers: it is what makes
// a screen reader announce the column name with each cell, and no caller
// had ever passed it. Still overridable via the spread.
export function TH({ children, className = '', scope = 'col', ...rest }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th scope={scope} className={clsx('px-3 py-2 font-medium whitespace-nowrap', className)} {...rest}>
      {children}
    </th>
  )
}

export function TD({ children, className = '', ...rest }: TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td className={clsx('px-3 py-2', className)} {...rest}>
      {children}
    </td>
  )
}
