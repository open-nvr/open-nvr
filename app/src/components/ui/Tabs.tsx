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

import { clsx } from 'clsx'
import type { ReactNode } from 'react'

export type TabItem = { key: string; label: ReactNode }

// Underline tab bar (extracted from the pattern used across Cloud/BYOK views)
// so any screen with local-state tabs shares one look. Colors are tokens only.
export function Tabs({
  tabs,
  active,
  onChange,
  className = '',
}: {
  tabs: TabItem[]
  active: string
  onChange: (key: string) => void
  className?: string
}) {
  return (
    <div
      role="tablist"
      className={clsx('flex gap-1 border-b border-[var(--border)] overflow-x-auto', className)}
    >
      {tabs.map((t) => (
        <button
          key={t.key}
          role="tab"
          aria-selected={active === t.key}
          onClick={() => onChange(t.key)}
          className={clsx(
            'px-3 py-2 text-sm whitespace-nowrap border-b-2 -mb-px transition-colors',
            active === t.key
              ? 'border-[var(--accent)] text-[var(--text)]'
              : 'border-transparent text-[var(--text-dim)] hover:text-[var(--text)]'
          )}
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}
