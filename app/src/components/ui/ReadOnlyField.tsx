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

import type { ReactNode } from 'react'

// A labelled value that is explicitly NOT editable — used for device data the
// operator can view but OpenNVR won't change (e.g. camera IP/network), and for
// read-only fields inside otherwise-editable tabs.
export function ReadOnlyField({
  label,
  value,
  mono = true,
}: {
  label: ReactNode
  value: ReactNode
  mono?: boolean
}) {
  const empty = value === null || value === undefined || value === ''
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-[var(--text-dim)]">{label}</span>
      <span
        className={`text-sm text-[var(--text)] break-all ${mono ? 'font-mono' : ''} ${empty ? 'text-[var(--text-dim)]' : ''}`}
      >
        {empty ? '—' : value}
      </span>
    </div>
  )
}
