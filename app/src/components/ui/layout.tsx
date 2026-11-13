// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Page-structure primitives: the pieces every page is assembled from, so a
 * section, a toolbar, a status chip or a loading state looks the same on every
 * screen. The dashboard's widget frame is the visual reference for Panel.
 */

import { clsx } from 'clsx'
import { NavLink } from 'react-router-dom'
import type { ButtonHTMLAttributes, ReactNode, Ref } from 'react'
import { EmptyState, ErrorCard, Skeleton } from './index'
import { useTranslation } from '../../i18n'
import { extractApiError } from '../../lib/apiError'

/* --------------------------- PageTitleBar -------------------------- */

/**
 * The title strip at the top of a page's first panel: accent icon, bold
 * title, the page's one line of purpose, optional actions on the right.
 * One height with or without actions (44px), so every page that uses it
 * starts at the same line — hand-built copies drifted by a few pixels each.
 * It only grows when a narrow screen wraps the actions onto a second line.
 * Sits inside a bordered panel; it draws only the divider beneath itself.
 */
export function PageTitleBar({ icon, title, description, actions }: {
  icon: ReactNode
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="min-h-11 flex flex-wrap items-center gap-x-2.5 gap-y-1.5 px-3 py-1.5 border-b border-[var(--border)] bg-[color-mix(in_oklab,var(--bg-2)_55%,var(--panel-2))]">
      <span className="shrink-0 flex text-[var(--accent)]" aria-hidden="true">{icon}</span>
      <h1 className="text-[15px] font-bold text-[var(--text)] whitespace-nowrap">{title}</h1>
      {description && (
        <p className="hidden md:block min-w-0 truncate text-sm font-medium text-[var(--text)]">{description}</p>
      )}
      {actions && <div className="ml-auto flex shrink-0 items-center gap-3">{actions}</div>}
    </div>
  )
}

/** The quiet accent action a PageTitleBar carries (e.g. "+ Add camera"). */
export const TITLE_ACTION_CLASS =
  'inline-flex h-7 items-center gap-1.5 border border-[var(--accent)] px-2.5 text-xs font-medium text-[var(--accent)] transition-colors hover:bg-[var(--accent)] hover:text-white disabled:cursor-not-allowed disabled:border-[var(--border)] disabled:text-[var(--text-dim)] disabled:hover:bg-transparent'

/** The body row under a PageTitleBar: same padding on every page. */
export const TITLE_PANEL_BODY_CLASS = 'px-3 py-2.5'

/* ------------------------------ Panel ------------------------------ */

/**
 * A titled section of a page: a slim header (icon, 11px uppercase title,
 * meta, actions) over a body. The dashboard widget look, for normal pages.
 * `flush` drops the body padding for edge-to-edge tables and lists.
 */
export function Panel({ title, icon, meta, actions, children, flush, className, bodyClassName, ...rest }: {
  title: ReactNode
  icon?: ReactNode
  meta?: ReactNode
  actions?: ReactNode
  children: ReactNode
  flush?: boolean
  className?: string
  bodyClassName?: string
  'data-testid'?: string
}) {
  return (
    <section
      className={clsx('border border-[var(--border)] bg-[var(--panel-2)] rounded-[var(--radius-card)] min-w-0', className)}
      data-testid={rest['data-testid']}
    >
      <header className="flex items-center gap-2 min-h-9 px-3 py-1.5 border-b border-[var(--border)] bg-[color-mix(in_oklab,var(--bg-2)_55%,var(--panel-2))]">
        {icon && <span className="flex shrink-0 text-[var(--text-dim)]">{icon}</span>}
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text)] truncate">{title}</h2>
        {meta && <div className="text-[11px] text-[var(--text-dim)] truncate min-w-0">{meta}</div>}
        {actions && <div className="ml-auto flex items-center gap-1.5 shrink-0">{actions}</div>}
      </header>
      <div className={clsx(flush ? '' : 'p-3', bodyClassName)}>{children}</div>
    </section>
  )
}

/* ---------------------------- IconButton --------------------------- */

const ICON_BUTTON_VARIANTS = {
  ghost: 'text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel)] border border-transparent',
  outline: 'text-[var(--text)] border border-[var(--border)] hover:bg-[var(--panel)]',
  danger: 'text-[var(--danger)] border border-transparent hover:bg-[color-mix(in_oklab,var(--danger)_14%,transparent)]',
} as const

/**
 * A button that is only an icon. `label` is required: it becomes both the
 * accessible name and the tooltip, because an icon alone tells a screen reader
 * nothing and tells a new operator little more.
 */
export function IconButton({ label, variant = 'ghost', size = 'sm', className, children, type = 'button', ...rest }:
  Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label'> & {
    label: string
    variant?: keyof typeof ICON_BUTTON_VARIANTS
    size?: 'xs' | 'sm'
    ref?: Ref<HTMLButtonElement>
  }) {
  return (
    <button
      type={type}
      aria-label={label}
      title={rest.title ?? label}
      className={clsx(
        'inline-grid place-items-center rounded-[var(--radius-button)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed',
        size === 'xs' ? 'h-6 w-6' : 'h-7 w-7',
        ICON_BUTTON_VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  )
}

/* ------------------------------ Spinner ---------------------------- */

export function Spinner({ size = 16, className, label }: { size?: number; className?: string; label?: string }) {
  return (
    <span
      role="status"
      aria-label={label}
      className={clsx('inline-block rounded-full border-2 border-[var(--border)] border-t-[var(--accent)] animate-spin', className)}
      style={{ width: size, height: size }}
    />
  )
}

/* ---------------------------- QueryState --------------------------- */

/**
 * Loading, error and empty, the same way everywhere. Takes a react-query
 * result or the plain `{ loading, error }` pair a page already keeps, so a
 * page can adopt it without changing how it fetches.
 *
 *     <QueryState query={camsQuery} isEmpty={(d) => d.cameras.length === 0}
 *                 empty={<EmptyState title="No cameras yet" />}>
 *       {(data) => <CameraTable rows={data.cameras} />}
 *     </QueryState>
 */
export function QueryState<T>({
  query,
  loading,
  error,
  data,
  onRetry,
  errorTitle,
  errorFallback,
  isEmpty,
  empty,
  skeleton,
  children,
}: {
  query?: { isPending: boolean; isError: boolean; error: unknown; data: T | undefined; refetch: () => unknown }
  loading?: boolean
  error?: unknown
  data?: T
  onRetry?: () => void
  errorTitle?: string
  /** Message when the error carries none of its own. */
  errorFallback?: string
  isEmpty?: (data: T) => boolean
  empty?: ReactNode
  /** Replaces the default skeleton block. */
  skeleton?: ReactNode
  children: (data: T) => ReactNode
}) {
  const { t } = useTranslation()
  const isLoading = query ? query.isPending : Boolean(loading)
  const err = query ? (query.isError ? query.error : null) : error
  const value = query ? query.data : data
  const retry = onRetry ?? (query ? () => { query.refetch() } : undefined)

  if (isLoading) return <>{skeleton ?? <Skeleton className="h-24" />}</>
  if (err) {
    return (
      <ErrorCard
        title={errorTitle ?? t('shared.somethingWentWrong')}
        message={extractApiError(err, errorFallback ?? t('shared.loadFailed'))}
        onRetry={retry}
      />
    )
  }
  if (value === undefined || value === null) return <>{skeleton ?? <Skeleton className="h-24" />}</>
  if (isEmpty?.(value)) return <>{empty ?? <EmptyState title={t('shared.nothingHere')} />}</>
  return <>{children(value)}</>
}

/* ------------------------------- Chip ------------------------------ */

const CHIP_TONES = {
  neutral: 'var(--text-dim)',
  ok: 'var(--ok)',
  warn: 'var(--warn)',
  danger: 'var(--danger)',
  accent: 'var(--accent)',
} as const

/**
 * A compact status or filter chip. With `onClick` it is a toggle (filter
 * chips); `selected` fills it. The tone dot carries state, and the text
 * always says it too -- colour is never the only signal.
 */
export function Chip({ children, tone = 'neutral', dot, selected, onClick, className }: {
  children: ReactNode
  tone?: keyof typeof CHIP_TONES
  dot?: boolean
  selected?: boolean
  onClick?: () => void
  className?: string
}) {
  const ink = CHIP_TONES[tone]
  const body = (
    <>
      {dot && <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: ink }} aria-hidden="true" />}
      {children}
    </>
  )
  const cls = clsx(
    'inline-flex items-center gap-1.5 h-6 px-2 text-xs border rounded-[var(--radius)] whitespace-nowrap',
    selected
      ? 'border-[var(--accent)] bg-[color-mix(in_oklab,var(--accent)_16%,transparent)] text-[var(--text)]'
      : 'border-[var(--border)] bg-[var(--panel)] text-[var(--text-dim)]',
    onClick && 'hover:text-[var(--text)] hover:border-[color-mix(in_oklab,var(--accent)_50%,var(--border))]',
    className,
  )
  return onClick ? (
    <button type="button" aria-pressed={selected} onClick={onClick} className={cls}>{body}</button>
  ) : (
    <span className={cls}>{body}</span>
  )
}

/* ----------------------------- FilterBar --------------------------- */

/**
 * The row above a list: search and filters on the left, actions on the
 * right, wrapping onto a second line on narrow screens instead of scrolling.
 */
export function FilterBar({ children, actions, className }: { children?: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <div className={clsx('flex flex-wrap items-center gap-2 mb-3', className)}>
      {children}
      {actions && <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

/* ----------------------------- RouteTabs --------------------------- */

/**
 * Tabs whose sections are routes (Settings, Access Control, Network). Same
 * look as `Tabs`, but each tab is a real link: the URL stays the source of
 * truth, so deep links, back/forward and the existing paths keep working.
 */
export function RouteTabs({ tabs, className }: {
  tabs: { to: string; label: ReactNode; end?: boolean }[]
  className?: string
}) {
  return (
    <nav className={clsx('flex gap-1 border-b border-[var(--border)] overflow-x-auto overflow-y-hidden', className)}>
      {tabs.map((tab) => (
        <NavLink
          key={tab.to}
          to={tab.to}
          end={tab.end}
          className={({ isActive }) => clsx(
            'px-3 py-2 text-sm whitespace-nowrap border-b-2 -mb-px transition-colors',
            isActive
              ? 'border-[var(--accent)] text-[var(--text)]'
              : 'border-transparent text-[var(--text-dim)] hover:text-[var(--text)]',
          )}
        >
          {tab.label}
        </NavLink>
      ))}
    </nav>
  )
}
