// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The header of an app's detail page — the same component whether the app
// is installed (AppView) or only listed in the catalog
// (UninstalledAppPage), so the two never drift apart again.
//
// One row: who the app is on the left, what you can do to it on the
// right. It used to be a tall hero with a separate back link, a stacked
// status column and full-size buttons, and it took as much height as the
// app's actual content.
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeft, Boxes } from 'lucide-react'
import { Badge, Card } from '../../components/ui'

export function AppHeader({
  name, category, version, badges, summary, meta, actions, children,
}: {
  name: string
  category?: string | null
  version?: string | null
  /** Extra badges after the version — status, pricing, "not installed". */
  badges?: ReactNode
  summary?: string | null
  /** A quiet line under the summary (provenance, links). */
  meta?: ReactNode
  /** Right-hand controls. Keep them `size="sm"`. */
  actions?: ReactNode
  /** Full-width content under the row: a notice, screenshots. */
  children?: ReactNode
}) {
  return (
    <Card className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
        <Link
          to="/app-catalog"
          className="grid h-8 w-8 shrink-0 place-items-center rounded text-[var(--text-dim)] hover:bg-[var(--panel)] hover:text-[var(--text)]"
          title="Back to App Catalog"
          aria-label="Back to App Catalog"
        >
          <ArrowLeft size={16} />
        </Link>
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-lg border border-[var(--border)] bg-[var(--bg-2)]">
          <Boxes size={20} className="text-[var(--accent,var(--text-dim))]" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h1 className="text-lg font-semibold leading-tight text-[var(--text)]">{name}</h1>
            {category && <Badge variant="info">{category}</Badge>}
            {version && <span className="text-xs text-[var(--text-dim)]">v{version}</span>}
            {badges}
          </div>
          {/* One line; the whole summary is a hover away. */}
          <p className="mt-0.5 truncate text-sm text-[var(--text-dim)]" title={summary || undefined}>
            {summary || 'No summary provided.'}
          </p>
          {meta && <div className="mt-1">{meta}</div>}
        </div>
        {actions && <div className="ml-auto flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {children}
    </Card>
  )
}
